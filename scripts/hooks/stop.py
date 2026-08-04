#!/usr/bin/env python3
"""Stop dispatcher (portable replacement for copilot-settings-stop.ps1).

On every stop, best-effort and always exiting 0, typecheck the frontend when skin
files changed.

Then a pre-stop verification phase (Tiers 1-3) reproduces the PR-gate checks
locally, scoped to the working-tree diff, and exits 2 to relay any failure back
into the session so it is fixed here instead of after a CI round-trip:
  - Tier 1: `lint-all.py --changed` (ruff/mypy/vulture/eslint/... , no infra),
  - Tier 2a: host `pytest scripts/hooks/tests/` when a scripts/ file changed --
    needs no Docker, so it runs even under a stack-down-by-default policy,
  - Tier 2b: host `pytest` (app/ or tests/ Python changed). With `[db] enabled`
    it runs against db+redis -- no app container, so the footprint is just
    db+redis. Uses them if up; else, only with `*_STOP_TESTS_AUTOSTART=1`, brings
    db+redis up on demand, runs, then stops what it started. Paid-safe via
    pytest.ini's `-m "not paid"`. Without a DB the suite needs no infra and simply
    runs,
  - Tier 3: `check-lock-markers.py` when a requirements file changed, and vitest
    when frontend/src changed.
It is loop-guarded (`stop_hook_active`), opt-out-able (`*_SKIP_STOP_VERIFY=1`, prefixed
per project — see `[project] env_prefix`),
gated on relevant files changing, and skips cleanly when tooling/infra is absent.

`skin_changed` and the verification helpers (`stop_hook_active`, `verify_enabled`, `changed_paths`,
`select_checks`, `run_checks`) are pure and unit-tested
(`scripts/hooks/tests/test_stop.py`); each external step is its own importable,
independently tested script.
"""

import contextlib
import functools
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

# scripts/hooks/ on path so the sibling, stdlib-only config helper imports before
# the venv (same pattern as branch-per-task.py's task_branch import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
# Everything project-specific below (env prefix, DB creds/ports, frontend layout,
# source-tree shape) is sourced from .devkit.toml so this
# script can be vendored unchanged across projects. See harness_config.py.
CFG = harness_config.load(REPO_ROOT)

# --- Pre-stop verification (Tiers 1-3) -------------------------------------
# Reproduce the PR-gate checks locally, scoped to the working-tree diff, so an
# agent fixes them in-session instead of after a CI round-trip. Each is gated on
# a relevant file changing; Tier 2b (DB tests) additionally needs db+redis
# reachable; all skip cleanly when their tooling/infra is absent.
LINT_ALL = REPO_ROOT / "scripts/lint-all.py"
CHECK_LOCK_MARKERS = REPO_ROOT / "scripts/check-lock-markers.py"

# Interpreter candidates for the verification checks, relative to the repo root.
VENV_PYTHONS = (".venv/Scripts/python.exe", ".venv/bin/python")
# PATH interpreters to try when there is no venv and the launcher cannot run the
# checks. `python3` is deliberately absent -- on Windows it is the Store shim this
# whole dance exists to escape, and on POSIX it is already `sys.executable`.
PATH_PYTHONS = ("python", "py")
# Importing this is the cheapest proof an interpreter can run the checks at all.
VERIFY_IMPORT = "pytest"


@functools.cache
def _can_verify(executable: str) -> bool:
    """True when `executable` can import what the verification checks need.

    Cached: `verify_python` is called once per check, and this spawns a process.
    """
    try:
        completed = subprocess.run(
            [executable, "-c", f"import {VERIFY_IMPORT}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False  # not executable, or hung -- either way, unusable.
    return completed.returncode == 0


def verify_python(repo_root: Path | None = None) -> str:
    """Interpreter for the verification checks -- one that can actually run them.

    The hooks are wired as `python3 <script>`, and on Windows `python3` resolves
    to the Microsoft Store shim: an interpreter with none of the project's
    dependencies installed. Running the checks under it fails on tooling rather
    than on the code -- `-m pytest` dies with "No module named pytest" and
    lint-all.py cannot import its linters -- which reads as a real CI failure and
    is unfixable by editing source.

    A venv wins outright: if one exists it is the project's declared environment,
    and a missing dependency *there* is a real provisioning failure worth
    surfacing. Without one, `sys.executable` cannot be trusted blindly -- it is
    the shim. A project with no venv is the normal case for a stdlib-only repo,
    not just the "fresh clone, CI" edge this once assumed, and on a Windows
    desktop the launcher is exactly the interpreter to avoid. So probe it, and
    fall through to PATH when it cannot import the tooling.

    Returns `sys.executable` when nothing can: the checks then fail loudly on
    tooling rather than silently reporting green having run nothing.

    `repo_root` defaults to REPO_ROOT at call time, not import time, so the
    module-level constant stays overridable.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    for rel in VENV_PYTHONS:
        candidate = root / rel
        if candidate.exists():
            return str(candidate)
    if _can_verify(sys.executable):
        return sys.executable
    for name in PATH_PYTHONS:
        found = shutil.which(name)
        if found and found != sys.executable and _can_verify(found):
            return found
    return sys.executable


# pytest's EXIT_NOTESTSCOLLECTED. Not a failure for this hook -- see _pytest_failures.
PYTEST_NO_TESTS_COLLECTED = 5

CHECK_LINT = "lint"  # Tier 1: lint-all.py --changed (no infra)
CHECK_SCRIPT_TESTS = "script-tests"  # Tier 2a: host pytest scripts/hooks/tests (no infra)
CHECK_TESTS = "tests"  # Tier 2b: host pytest tests/ against db+redis (paid-safe)
CHECK_LOCKS = "lock-markers"  # Tier 3: check-lock-markers.py (deps changed)
CHECK_FRONTEND = "frontend"  # Tier 3: vitest (frontend/src changed)

# Files whose change means "dependencies moved, re-verify the locks". Covers both
# dependency models the harness supports: pip-tools (`requirements*.in/.txt`) and
# uv/poetry (a single committed lockfile). Matching only `requirements*` left the
# lock-marker tier silently inert in every uv-native project — it never fired, so
# nothing looked broken.
_REQ_RE = re.compile(r"(^|/)(requirements[^/]*\.(in|txt)|uv\.lock|poetry\.lock)$")

# Harness control env vars, prefixed per project (CFG.env_prefix, e.g. CARAMELI):
#   *_SKIP_STOP_VERIFY -- opt out of pre-stop verification entirely.
#   *_STOP_TESTS_AUTOSTART -- opt in to Tier 2b bringing up ONLY db+redis on
#     demand when app/tests changed and they are down, running host pytest, then
#     stopping only what it started. Off by default so the stack-down-to-save-
#     memory policy holds. Host pytest (no app container) keeps the peak footprint
#     at db+redis (~0.75 GB) vs the full stack (~4 GB); tests inherit pytest.ini's
#     `-m "not paid"` so they can never bill a live provider.
SKIP_VERIFY_ENV = CFG.env("SKIP_STOP_VERIFY")
AUTOSTART_ENV = CFG.env("STOP_TESTS_AUTOSTART")


def _read_stdin() -> str:
    """Best-effort read of the hook payload; '' when stdin is a tty or unreadable.

    Decodes the raw stdin bytes as UTF-8 (the encoding Claude Code writes the hook
    payload in) with surrogateescape, instead of trusting the process locale. On
    Windows that locale is cp1252, which mis-decodes non-ASCII payload bytes into
    lone surrogates on read and then raises UnicodeEncodeError when the same string
    is written to the archive child (position-2642 '\\udc9d' crash). surrogateescape
    lets any byte round-trip back out unchanged when re-encoded for the child.
    """
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            return buffer.read().decode("utf-8", errors="surrogateescape")
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def skin_changed(porcelain: str) -> bool:
    """True when `git status --porcelain -- frontend/src/skins` reported changes."""
    return any(line.strip() for line in porcelain.splitlines())


def _git_skin_status(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", CFG.frontend.skin],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""


# --- Pre-stop verification helpers (pure; unit-tested in test_stop.py) ------


def stop_hook_active(raw_stdin: str) -> bool:
    """True when this stop is already a continuation triggered by a stop hook.

    Claude Code sets `stop_hook_active` on the payload once a Stop hook has
    blocked and the agent resumed. Honouring it prevents an infinite
    fix -> stop -> block loop: verification runs on the first stop only.
    """
    try:
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(isinstance(payload, dict) and payload.get("stop_hook_active"))


def verify_enabled(env: Mapping[str, str]) -> bool:
    """False when the operator has opted out of pre-stop verification."""
    return env.get(SKIP_VERIFY_ENV) != "1"


def changed_paths(porcelain: str) -> list[str]:
    """Repo-relative paths from `git status --porcelain` (handles renames/quotes)."""
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.strip()
        if " -> " in rest:  # rename: "R  old -> new" -> keep the new path
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip().strip('"'))
    return paths


def _is_py(path: str) -> bool:
    return path.endswith((".py", ".pyi"))


def _is_frontend(path: str) -> bool:
    return path.startswith(CFG.frontend.src)


def _is_reqs(path: str) -> bool:
    return bool(_REQ_RE.search(path))


def _is_script(path: str) -> bool:
    """A Python file under scripts/ -- covered by the infra-free host test suite."""
    return path.startswith("scripts/") and _is_py(path)


def host_test_targets(paths: list[str]) -> list[str]:
    """pytest targets for the DB tier, or [] when no app/tests Python changed.

    An app/ change can break tests anywhere, and without testmon we cannot map it
    to specific tests -- run the whole unit suite. A tests-only change runs just
    the changed test files (fast, precise).
    """
    if any(_is_py(p) and p.startswith(CFG.app_dir) for p in paths):
        return [CFG.unit_tests]
    return sorted(p for p in paths if p.startswith(CFG.tests_dir) and _is_py(p))


def select_checks(paths: list[str]) -> list[str]:
    """The infra-light checks to run for this diff (the DB tier is separate).

    - lint runs whenever anything changed (lint-all.py --changed self-scopes
      internally, so an irrelevant edit is a fast no-op).
    - script-tests (host pytest scripts/hooks/tests) run when a scripts/ file
      changed. These need no Docker, so they run even with the stack down.
    - lock-markers run when a requirements file changed.
    - frontend (vitest) runs when frontend/src changed; tsc/eslint are already
      covered by lint's changed-scope, so this adds only the unit tests.

    The DB-backed tier (app/ or tests/ Python) is handled by run_db_tests(),
    which needs db+redis and so has its own reachability/autostart gating.
    """
    if not paths:
        return []
    checks = [CHECK_LINT]
    if any(_is_script(p) for p in paths):
        checks.append(CHECK_SCRIPT_TESTS)
    if any(_is_reqs(p) for p in paths):
        checks.append(CHECK_LOCKS)
    if CFG.frontend.enabled and any(_is_frontend(p) for p in paths):
        checks.append(CHECK_FRONTEND)
    return checks


def _git_status_porcelain(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def autostart_enabled(env: Mapping[str, str]) -> bool:
    """True when the operator opted into on-demand db+redis autostart."""
    return env.get(AUTOSTART_ENV) == "1"


def services_to_stop(before: set[str], after: set[str]) -> list[str]:
    """Services this hook started (running now, not before) -- what to stop again."""
    return sorted(after - before)


def _compose_running_services(repo_root: Path = REPO_ROOT) -> set[str]:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--services", "--status", "running"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return set(result.stdout.split()) if result.returncode == 0 else set()


def db_redis_running(repo_root: Path = REPO_ROOT) -> bool:
    """True when both db and redis are up (whether via the full stack or db+redis
    alone) -- host pytest can reach them either way."""
    return set(CFG.db.services).issubset(_compose_running_services(repo_root))


def _compose_up_db_redis(repo_root: Path = REPO_ROOT) -> bool:
    """Bring up ONLY db+redis, waiting for health. False on failure (daemon down,
    timeout) so the DB tier skips instead of blocking."""
    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait", *CFG.db.services],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _compose_stop(services: list[str], repo_root: Path = REPO_ROOT) -> None:
    """Stop (not remove) the given services, freeing their memory. Best-effort."""
    if not services:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["docker", "compose", "stop", *services],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )


def _parse_host_port(output: str) -> str | None:
    """Extract the host port from `docker compose port` output (e.g. '0.0.0.0:5432',
    '[::]:5432', '127.0.0.1:5433')."""
    line = next((ln for ln in reversed(output.splitlines()) if ln.strip()), "")
    if ":" not in line:
        return None
    port = line.rsplit(":", 1)[1].strip()
    return port if port.isdigit() else None


def _compose_host_port(
    service: str, container_port: int, repo_root: Path = REPO_ROOT
) -> str | None:
    try:
        result = subprocess.run(
            ["docker", "compose", "port", service, str(container_port)],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_host_port(result.stdout) if result.returncode == 0 else None


def host_db_env(repo_root: Path = REPO_ROOT) -> dict[str, str] | None:
    """Env for host pytest to reach the containers over their published ports, or
    None when a port cannot be resolved (containers not actually up)."""
    db = CFG.db
    db_port = _compose_host_port(db.db_service, db.db_port, repo_root)
    redis_port = _compose_host_port(db.redis_service, db.redis_port, repo_root)
    if not db_port or not redis_port:
        return None
    db_url = f"{db.url_scheme}://{db.user}:{db.password}@localhost:{db_port}/{db.name}"
    env: dict[str, str] = {name: db_url for name in db.url_env}
    env[db.redis_env] = f"redis://localhost:{redis_port}"
    # Extra secrets host pytest needs; an already-set os.environ value wins.
    for name, default in db.test_env.items():
        env[name] = os.environ.get(name, default)
    return env


def _pytest_failures(
    targets: list[str], repo_root: Path, extra_env: dict[str, str] | None = None
) -> list[tuple[str, str | None, str]]:
    """Run host pytest over `targets`; [] on pass, one CHECK_TESTS failure otherwise.

    Shared by both shapes of Tier 2b (with and without a DB), so the two differ only
    in the infra they arrange and the env they inject -- not in how a failure is
    captured and reported. An OS error is a skip: verification never blocks the agent
    over a local tooling gap.
    """
    argv = [verify_python(), "-m", "pytest", *targets, "-q"]
    try:
        result = subprocess.run(
            argv,
            cwd=repo_root,
            capture_output=True,
            text=True,
            env={**os.environ, **extra_env} if extra_env else None,
        )
    except OSError:
        return []
    # 5 is pytest's EXIT_NOTESTSCOLLECTED, and it is not a failure here. Targets come
    # from `host_test_targets`, which selects *changed files* under tests/ -- so editing
    # a helper that holds no tests of its own (conftest.py, a support module) hands
    # pytest a file with nothing to collect. Treating that as a failure blocks the stop
    # with "no tests ran", which no source edit can resolve.
    if result.returncode in (0, PYTEST_NO_TESTS_COLLECTED):
        return []
    tail = (result.stdout + result.stderr).strip().splitlines()[-20:]
    return [(CHECK_TESTS, None, "\n".join(tail))]


def run_host_tests(
    paths: list[str], env: Mapping[str, str], repo_root: Path = REPO_ROOT
) -> list[tuple[str, str | None, str]]:
    """Tier 2b: host pytest for changed app/tests code, DB or no DB.

    Two shapes, one tier, and the second one is why this wrapper exists. With a DB
    configured the suite needs db+redis, so it goes through `run_db_tests`'s
    reachability/autostart/teardown dance. With `[db] enabled = false` the suite needs
    no infra at all and simply runs.

    Without that second branch a DB-less project ran **no application tests at all**
    on Stop: this is the only tier that consults `host_test_targets`, and it returned
    early on `not CFG.db.enabled`. So every `bare`-preset project -- and devkit itself,
    whose `tests/` tree is the generator's entire safety net -- got lint and the
    vendored hook tests, silently never its own suite, and only found a broken test in
    CI. The DB tier's gating is about *infra*; it should never have been the thing that
    decided whether tests run.
    """
    if CFG.db.enabled:
        return run_db_tests(paths, env, repo_root)
    targets = host_test_targets(paths)
    if not targets:
        return []
    return _pytest_failures(targets, repo_root)


def run_db_tests(
    paths: list[str], env: Mapping[str, str], repo_root: Path = REPO_ROOT
) -> list[tuple[str, str | None, str]]:
    """Tier 2b with a DB: host pytest for changed app/tests against db+redis.

    Runs on the host (no app container) so the footprint is just db+redis. Uses
    them if already up; otherwise, only with autostart opted in, brings up db+redis,
    runs, then stops exactly what it started. Any infra gap (daemon down, up
    failure, unresolved ports) is a clean skip -> deferred to CI, never a block.

    Callers should prefer `run_host_tests`, which routes to this only when a DB is
    actually configured.
    """
    if not CFG.db.enabled:
        return []
    targets = host_test_targets(paths)
    if not targets:
        return []

    started: list[str] = []
    if not db_redis_running(repo_root):
        if not autostart_enabled(env):
            return []
        before = _compose_running_services(repo_root)
        if not _compose_up_db_redis(repo_root):
            return []
        started = services_to_stop(before, _compose_running_services(repo_root))

    try:
        db_env = host_db_env(repo_root)
        if db_env is None:
            return []
        return _pytest_failures(targets, repo_root, db_env)
    finally:
        _compose_stop(started, repo_root)


def _command_for(name: str) -> tuple[list[str], Path, str | None] | None:
    """(argv, cwd, artifact_path) for a check, or None when its tool is absent.

    "Absent" includes a *script* that this project does not have. Returning an argv
    for a missing script does not skip the check -- the interpreter exits 2 with
    "can't open file", which `run_checks` cannot tell apart from a real finding and
    reports as a failure the agent has no way to fix. That is not hypothetical: no
    generated project ships `check-lock-markers.py`, so every one of them blocked its
    own Stop with a bogus `lock-markers` failure the moment a lockfile changed.

    Deciding absence here also makes it a readable state rather than an accident, which
    is what lets CI hold a project to it: `scripts/hooks/tests/test_repo_contract.py`
    fails when a script this project's own config makes reachable is missing.
    """
    if name == CHECK_LINT:
        if not LINT_ALL.exists():
            return None
        # --no-secrets is part of the contract between this hook and lint-all.py: a
        # project whose lint runner has a detect-secrets pass skips it here (it is the
        # pre-commit hook's job, and running it churns .secrets.baseline every turn),
        # and one without a secrets pass accepts the flag as a documented no-op. Either
        # way lint-all.py must *parse* it -- argparse rejecting it exits 2, which reads
        # as a permanent lint failure on every single Stop.
        return (
            [verify_python(), str(LINT_ALL), "--changed", "--no-secrets"],
            REPO_ROOT,
            "logs/lint-errors.log",
        )
    if name == CHECK_SCRIPT_TESTS:
        return ([verify_python(), "-m", "pytest", "scripts/hooks/tests/", "-q"], REPO_ROOT, None)
    if name == CHECK_LOCKS:
        # Optional tier: the script is project-owned (its sentinels name that
        # project's lockfiles), so a project without one simply has no tier.
        if not CHECK_LOCK_MARKERS.exists():
            return None
        return ([verify_python(), str(CHECK_LOCK_MARKERS)], REPO_ROOT, None)
    if name == CHECK_FRONTEND:
        npm = shutil.which("npm")
        if not npm:
            return None
        return ([npm, *CFG.frontend.test_cmd], REPO_ROOT / CFG.frontend.dir, None)
    return None


def run_checks(names: list[str]) -> list[tuple[str, str | None, str]]:
    """Run selected checks; return (name, artifact, tail) for each that failed.

    A missing tool or an OS error is a skip, never a failure: verification must
    never block the agent because of a local tooling gap.
    """
    failures: list[tuple[str, str | None, str]] = []
    for name in names:
        spec = _command_for(name)
        if spec is None:
            continue
        argv, cwd, artifact = spec
        try:
            result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
        except OSError:
            continue
        if result.returncode != 0:
            tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
            failures.append((name, artifact, "\n".join(tail)))
    return failures


def _print_verify_failures(failures: list[tuple[str, str | None, str]]) -> None:
    lines = ["Pre-stop verification found issues that would fail CI -- fix before finishing:"]
    for name, artifact, tail in failures:
        if artifact and (REPO_ROOT / artifact).exists():
            lines.append(f"  - {name}: see {artifact}")
        else:
            lines.append(f"  - {name}:")
            if tail:
                lines.extend(f"      {ln}" for ln in tail.splitlines())
    lines.append(
        "Re-run locally: python scripts/lint-all.py --changed | python scripts/run-tests.py --changed"
    )
    lines.append(f"(Set {SKIP_VERIFY_ENV}=1 to skip this gate.)")
    print("\n".join(lines), file=sys.stderr)


def verify(raw_stdin: str, env: Mapping[str, str]) -> int:
    """Run pre-stop verification. Returns 2 (block, relay) on failure, else 0."""
    if stop_hook_active(raw_stdin) or not verify_enabled(env):
        return 0
    paths = changed_paths(_git_status_porcelain(REPO_ROOT))
    # Infra-light tiers (lint, script-tests, locks, frontend) plus the application
    # test tier, which arranges its own db+redis reachability/autostart/teardown when
    # this project has a DB and just runs pytest when it does not.
    failures = run_checks(select_checks(paths))
    failures += run_host_tests(paths, env)
    if failures:
        _print_verify_failures(failures)
        return 2
    return 0


def main() -> int:
    raw_stdin = _read_stdin()

    if CFG.frontend.enabled and skin_changed(_git_skin_status(REPO_ROOT)):
        npm = shutil.which("npm")
        if npm:
            subprocess.run(
                [npm, *CFG.frontend.typecheck_cmd],
                cwd=REPO_ROOT / CFG.frontend.dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    # Pre-stop verification runs last: it may exit 2 to block the stop and relay
    # failures back into the session. All best-effort side effects above have
    # already run and always exit 0, so a blocked stop never loses that work.
    return verify(raw_stdin, os.environ)


if __name__ == "__main__":
    sys.exit(main())
