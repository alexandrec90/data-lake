"""Unit tests for the portable Stop hook.

**This file is vendored into every consuming project.** Every value that varies per
project — the control-env prefix, `app/`, the DB credentials, whether a frontend
exists — must come from `hook.CFG` (which the hook itself reads from that project's
`.devkit.toml`), never from a literal. Hard-coding carameli's values here
made the vendored suite fail in any repo shaped differently, which is what the
config seam exists to prevent.
"""

import io
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import load_module

hook = load_module("scripts/hooks/stop.py")

# The shape of the project this suite is running inside. Read once so the intent of
# each assertion below stays visible.
CFG = hook.CFG
APP_FILE = f"{CFG.app_dir}main.py"

# `run_db_tests` returns early when the project declares no DB tier, so the tests
# that assert it *does* work have nothing to observe there. Skipping is right rather
# than asserting the early return: that path is already covered by
# `test_run_db_tests_skips_when_down_and_not_opted_in`.
requires_db = pytest.mark.skipif(not CFG.db.enabled, reason="project has no DB test tier")


class _FakeStdin:
    """Minimal stdin stand-in exposing .buffer/.isatty()/.read() for _read_stdin."""

    def __init__(self, data: bytes, tty: bool = False):
        self.buffer = io.BytesIO(data)
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self):
        return self.buffer.read().decode("utf-8", "surrogateescape")


def test_skin_changed_detects_porcelain_lines():
    assert hook.skin_changed(" M frontend/src/skins/carameli/Tile.tsx\n") is True
    assert hook.skin_changed("") is False
    assert hook.skin_changed("\n  \n") is False


def test_read_stdin_decodes_utf8_payload(monkeypatch):
    payload = '{"transcript_path": "/x/sesión.jsonl"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload.encode("utf-8")))
    assert hook._read_stdin() == payload


def test_read_stdin_survives_undecodable_byte(monkeypatch):
    # A lone 0x9d byte is undefined in cp1252 and invalid UTF-8. The reader must
    # not crash on it, and the byte must round-trip back out unchanged. Regression
    # for the stop-hook UnicodeEncodeError: 'charmap' codec can't encode '\udc9d'.
    raw = b'{"transcript_path": "/x/a\x9d.jsonl"}'
    monkeypatch.setattr(sys, "stdin", _FakeStdin(raw))
    result = hook._read_stdin()
    assert result.encode("utf-8", "surrogateescape") == raw


def test_read_stdin_empty_for_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(b'{"x": 1}', tty=True))
    assert hook._read_stdin() == ""


# --- pre-stop verification --------------------------------------------------


def test_stop_hook_active_true_only_when_flagged():
    assert hook.stop_hook_active('{"stop_hook_active": true}') is True
    assert hook.stop_hook_active('{"stop_hook_active": false}') is False
    assert hook.stop_hook_active('{"cwd": "/repo"}') is False
    assert hook.stop_hook_active("not json") is False
    assert hook.stop_hook_active("[1, 2]") is False


def test_verify_enabled_opt_out():
    assert hook.verify_enabled({}) is True
    assert hook.verify_enabled({hook.SKIP_VERIFY_ENV: "0"}) is True
    assert hook.verify_enabled({hook.SKIP_VERIFY_ENV: "1"}) is False


def test_changed_paths_parses_status_and_renames():
    porcelain = " M app/main.py\n?? scripts/new.py\nR  app/old.py -> app/renamed.py\n\n"
    assert hook.changed_paths(porcelain) == [
        "app/main.py",
        "scripts/new.py",
        "app/renamed.py",
    ]


def test_changed_paths_empty():
    assert hook.changed_paths("") == []


def test_path_predicates():
    assert hook._is_py("app/x.py") and hook._is_py("app/y.pyi")
    assert not hook._is_py("README.md")
    assert hook._is_frontend("frontend/src/App.tsx")
    assert not hook._is_frontend("frontend/vite.config.ts")
    assert hook._is_reqs("requirements.txt")
    assert hook._is_reqs("requirements-dev.in")
    assert not hook._is_reqs("app/requirements_notes.md")
    assert hook._is_script("scripts/hooks/stop.py")
    assert not hook._is_script("scripts/notes.md")


def test_host_test_targets_app_change_runs_whole_unit_suite():
    # An application-code change can break tests anywhere -> whole unit suite.
    unit = [CFG.unit_tests]
    assert hook.host_test_targets([APP_FILE]) == unit
    assert hook.host_test_targets([APP_FILE, f"{CFG.unit_tests}/t.py"]) == unit


def test_host_test_targets_tests_only_runs_changed_files():
    assert hook.host_test_targets(["tests/unit/test_a.py", "tests/integration/test_b.py"]) == [
        "tests/integration/test_b.py",
        "tests/unit/test_a.py",
    ]


def test_host_test_targets_no_app_or_tests_is_empty():
    # The paths must fall outside BOTH app_dir and tests_dir, and cannot be hardcoded:
    # this file is vendored, and the literal `scripts/hooks/stop.py` this test used to
    # pass *is* application code in a project whose `app_dir` is `scripts/` — devkit's
    # is, so the assertion inverted there. The precondition is asserted rather than
    # assumed, so a manifest that ever makes these paths meaningful fails loudly.
    unrelated = ["docs/notes.md", "third_party/x.py"]
    for path in unrelated:
        assert not path.startswith((CFG.app_dir, CFG.tests_dir)), f"{path} is not neutral here"
    assert hook.host_test_targets(unrelated) == []


def test_select_checks_empty_diff_runs_nothing():
    assert hook.select_checks([]) == []


def test_select_checks_never_includes_db_tier():
    # The DB tier is handled by run_db_tests, not select_checks.
    assert hook.select_checks(["app/main.py"]) == [hook.CHECK_LINT]
    assert hook.CHECK_TESTS not in hook.select_checks(["tests/unit/t.py"])


def test_select_checks_scripts_run_host_tests():
    # A scripts/ change is verified on the host with no Docker.
    assert hook.select_checks(["scripts/hooks/stop.py"]) == [
        hook.CHECK_LINT,
        hook.CHECK_SCRIPT_TESTS,
    ]


@pytest.mark.parametrize(
    "changed",
    [
        "requirements.txt",
        "requirements.in",
        "requirements-dev.txt",
        # uv/poetry projects express the same event as one lockfile. Before these,
        # _REQ_RE matched only `requirements*`, so the tier never fired for them --
        # an inert check looks exactly like a passing one.
        "uv.lock",
        "poetry.lock",
        "subdir/uv.lock",
    ],
)
def test_select_checks_dependency_change_adds_lock_markers(changed):
    checks = hook.select_checks([changed])
    assert hook.CHECK_LOCKS in checks and hook.CHECK_TESTS not in checks


@pytest.mark.parametrize(
    "changed",
    [
        "my_uv.lock",  # must anchor on a path boundary, not any substring
        "docs/uv.lock.md",  # ...and on end-of-name
        "notes/requirements.md",
        "uv.locked",
    ],
)
def test_select_checks_ignores_lockfile_lookalikes(changed):
    assert hook.CHECK_LOCKS not in hook.select_checks([changed])


@pytest.mark.skipif(not CFG.frontend.enabled, reason="project has no frontend tier")
def test_select_checks_frontend_adds_vitest():
    assert hook.select_checks([f"{CFG.frontend.src}App.tsx"]) == [
        hook.CHECK_LINT,
        hook.CHECK_FRONTEND,
    ]


def test_select_checks_non_relevant_change_runs_lint_only():
    # A docs edit still runs lint (lint-all --changed self-scopes to a no-op),
    # but never script-tests / locks / frontend.
    assert hook.select_checks(["README.md"]) == [hook.CHECK_LINT]


def test_parse_host_port_variants():
    assert hook._parse_host_port("0.0.0.0:5432\n") == "5432"
    assert hook._parse_host_port("[::]:5432") == "5432"
    assert hook._parse_host_port("127.0.0.1:5433") == "5433"
    # Multi-line (IPv4 + IPv6): last non-empty line wins, still a valid port.
    assert hook._parse_host_port("0.0.0.0:5432\n[::]:5432\n") == "5432"
    assert hook._parse_host_port("") is None
    assert hook._parse_host_port("garbage") is None


def test_run_checks_skips_missing_tool(monkeypatch):
    monkeypatch.setattr(hook, "_command_for", lambda name: None)
    assert hook.run_checks([hook.CHECK_FRONTEND]) == []


def test_run_checks_collects_failures(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "_command_for", lambda name: (["true"], hook.REPO_ROOT, None))
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *a, **k: sp.CompletedProcess([], 1, "boom\n", "bad line\n"),
    )
    failures = hook.run_checks([hook.CHECK_LINT])
    assert len(failures) == 1
    assert failures[0][0] == hook.CHECK_LINT
    assert "bad line" in failures[0][2]


def test_run_checks_oserror_is_skip_not_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("no such tool")

    monkeypatch.setattr(hook, "_command_for", lambda name: (["nope"], hook.REPO_ROOT, None))
    monkeypatch.setattr(hook.subprocess, "run", boom)
    assert hook.run_checks([hook.CHECK_LINT]) == []


def test_command_for_returns_none_when_a_repo_script_is_absent(tmp_path, monkeypatch):
    """Absence is decided here, not left to the OSError handler in `run_checks`.

    Both skip the check, so this changes no behaviour — it changes what the skip
    *means*. Resolved to None, "this project has no such tier" is a readable state
    that `test_repo_contract.py` can then hold the project to; reached as an OSError
    from spawning a missing file, it is indistinguishable from an installed script
    that crashed on startup.
    """
    missing = tmp_path / "gone.py"
    monkeypatch.setattr(hook, "LINT_ALL", missing)
    monkeypatch.setattr(hook, "CHECK_LOCK_MARKERS", missing)
    assert hook._command_for(hook.CHECK_LINT) is None
    assert hook._command_for(hook.CHECK_LOCKS) is None
    # The tier that needs no project-owned script is unaffected.
    assert hook._command_for(hook.CHECK_SCRIPT_TESTS) is not None


def test_verify_skips_when_loop_active(monkeypatch):
    monkeypatch.setattr(hook, "run_checks", lambda names: [("lint", None, "x")])
    assert hook.verify('{"stop_hook_active": true}', {}) == 0


def test_verify_skips_when_opted_out(monkeypatch):
    monkeypatch.setattr(hook, "run_checks", lambda names: [("lint", None, "x")])
    assert hook.verify("{}", {hook.SKIP_VERIFY_ENV: "1"}) == 0


def test_verify_returns_two_on_failure(monkeypatch):
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    monkeypatch.setattr(hook, "run_checks", lambda names: [("lint", "logs/lint-errors.log", "")])
    assert hook.verify("{}", {}) == 2


def test_verify_returns_two_when_db_tests_fail(monkeypatch):
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "run_checks", lambda names: [])
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [("tests", None, "F app/x")])
    assert hook.verify("{}", {}) == 2


def test_verify_returns_zero_when_clean(monkeypatch):
    monkeypatch.setattr(hook, "_git_status_porcelain", lambda root: " M app/main.py\n")
    monkeypatch.setattr(hook, "run_checks", lambda names: [])
    monkeypatch.setattr(hook, "run_host_tests", lambda *a, **k: [])
    assert hook.verify("{}", {}) == 0


# --- Tier 2b: host pytest against db+redis (+ opt-in autostart) -------------


def test_autostart_enabled_opt_in():
    assert hook.autostart_enabled({}) is False
    assert hook.autostart_enabled({hook.AUTOSTART_ENV: "0"}) is False
    assert hook.autostart_enabled({hook.AUTOSTART_ENV: "1"}) is True


def test_services_to_stop_only_newly_started():
    assert hook.services_to_stop({"redis"}, {"redis", "db"}) == ["db"]
    assert hook.services_to_stop({"db", "redis"}, {"db", "redis"}) == []


def test_db_redis_running_needs_every_configured_service(monkeypatch):
    configured = set(CFG.db.services)
    monkeypatch.setattr(
        hook, "_compose_running_services", lambda *a, **k: configured | {"unrelated"}
    )
    assert hook.db_redis_running() is True
    # Drop one required service: the tier must not run against a half-up stack.
    partial = configured - {CFG.db.db_service}
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: partial)
    assert hook.db_redis_running() is False


def test_host_db_env_builds_urls_from_ports(monkeypatch):
    db = CFG.db
    monkeypatch.setattr(
        hook, "_compose_host_port", lambda svc, port, *a: "5599" if svc == db.db_service else "6699"
    )
    env = hook.host_db_env()
    expected = f"{db.url_scheme}://{db.user}:{db.password}@localhost:5599/{db.name}"
    # Every configured alias gets the same URL — carameli exposes two.
    for name in db.url_env:
        assert env[name] == expected
    if db.redis_service in db.services:
        assert env[db.redis_env] == "redis://localhost:6699"


def test_host_db_env_none_when_port_unresolved(monkeypatch):
    monkeypatch.setattr(hook, "_compose_host_port", lambda svc, port, *a: None)
    assert hook.host_db_env() is None


def test_run_db_tests_no_targets_never_touches_docker(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not run")

    monkeypatch.setattr(hook, "db_redis_running", boom)
    assert hook.run_db_tests(["scripts/x.py", "README.md"], {}) == []


@requires_db
def test_run_db_tests_runs_when_db_up_no_autostart(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    up = {"called": False}
    monkeypatch.setattr(
        hook, "_compose_up_db_redis", lambda *a, **k: up.__setitem__("called", True)
    )
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 0))

    assert hook.run_db_tests([APP_FILE], {}) == []
    assert up["called"] is False  # already up -> no autostart
    assert stopped["svc"] == []  # started nothing -> stop nothing


@requires_db
def test_run_db_tests_reports_pytest_failure(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    monkeypatch.setattr(hook, "_compose_stop", lambda *a, **k: None)
    monkeypatch.setattr(
        hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 1, "F tests/unit/x\n", "")
    )
    failures = hook.run_db_tests(["tests/unit/x.py"], {})
    assert failures and failures[0][0] == hook.CHECK_TESTS
    assert "tests/unit/x" in failures[0][2]


def test_run_db_tests_skips_when_down_and_not_opted_in(monkeypatch):
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    up = {"called": False}
    monkeypatch.setattr(
        hook, "_compose_up_db_redis", lambda *a, **k: up.__setitem__("called", True)
    )
    assert hook.run_db_tests(["app/main.py"], {}) == []
    assert up["called"] is False  # no opt-in -> never autostarts


@requires_db
def test_run_db_tests_autostarts_and_stops_only_started(monkeypatch):
    import subprocess as sp

    # The invariant: the hook leaves the stack as it found it. Whatever was already
    # running stays running; only what this run started is stopped again.
    configured = set(CFG.db.services)
    already_up = configured - {CFG.db.db_service}
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    running = iter([already_up, configured])  # before up, after up
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: next(running))
    monkeypatch.setattr(hook, "_compose_up_db_redis", lambda *a, **k: True)
    monkeypatch.setattr(hook, "host_db_env", lambda *a, **k: {"DATABASE_URL": "x"})
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: sp.CompletedProcess([], 0))
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))

    assert hook.run_db_tests([APP_FILE], {hook.AUTOSTART_ENV: "1"}) == []
    assert stopped["svc"] == [CFG.db.db_service]  # only the newly-started service


@requires_db
def test_run_db_tests_up_failure_skips_and_stops_nothing(monkeypatch):
    monkeypatch.setattr(hook, "db_redis_running", lambda *a, **k: False)
    monkeypatch.setattr(hook, "_compose_running_services", lambda *a, **k: set())
    monkeypatch.setattr(hook, "_compose_up_db_redis", lambda *a, **k: False)  # daemon down
    stopped = {}
    monkeypatch.setattr(hook, "_compose_stop", lambda svc, *a, **k: stopped.setdefault("svc", svc))

    assert hook.run_db_tests([APP_FILE], {hook.AUTOSTART_ENV: "1"}) == []
    assert "svc" not in stopped  # returns before try/finally -> _compose_stop never called


# --- verify_python: run the checks in the venv, not the launching interpreter ---
# The hooks are wired as `python3 <script>`; on Windows that resolves to the
# Microsoft Store shim, which has no pytest and none of the project's linters, so
# every check failed on tooling rather than on the code.


def test_verify_python_prefers_windows_venv(tmp_path):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")

    assert hook.verify_python(tmp_path) == str(py)


def test_verify_python_prefers_posix_venv(tmp_path):
    py = tmp_path / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("")

    assert hook.verify_python(tmp_path) == str(py)


def test_verify_python_falls_back_to_launcher_without_venv(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "_can_verify", lambda exe: True)
    assert hook.verify_python(tmp_path) == sys.executable


def test_verify_python_skips_a_launcher_that_cannot_import_the_tooling(tmp_path, monkeypatch):
    # Regression: with no venv, the fallback returned sys.executable unconditionally
    # -- which on Windows IS the Store shim the function exists to avoid, so every
    # check died with "No module named pytest". A stdlib-only project has no venv as
    # its normal state, so this is not just a fresh-clone edge.
    usable = str(tmp_path / "real-python")
    monkeypatch.setattr(hook, "_can_verify", lambda exe: exe != sys.executable)
    monkeypatch.setattr(hook.shutil, "which", lambda name: usable if name == "python" else None)

    assert hook.verify_python(tmp_path) == usable


def test_verify_python_never_probes_python3(tmp_path, monkeypatch):
    # `python3` is the shim being escaped; probing it could hand back the very
    # interpreter that has no pytest.
    assert "python3" not in hook.PATH_PYTHONS

    probed = []
    monkeypatch.setattr(hook, "_can_verify", lambda exe: False)
    monkeypatch.setattr(hook.shutil, "which", lambda name: probed.append(name) or None)
    hook.verify_python(tmp_path)

    assert "python3" not in probed


def test_verify_python_tries_py_launcher_when_python_is_absent(tmp_path, monkeypatch):
    usable = str(tmp_path / "py-launcher")
    monkeypatch.setattr(hook, "_can_verify", lambda exe: exe == usable)
    monkeypatch.setattr(hook.shutil, "which", lambda name: usable if name == "py" else None)

    assert hook.verify_python(tmp_path) == usable


def test_verify_python_returns_launcher_when_nothing_can_verify(tmp_path, monkeypatch):
    # Better to fail loudly on tooling than to report green having run nothing.
    monkeypatch.setattr(hook, "_can_verify", lambda exe: False)
    monkeypatch.setattr(hook.shutil, "which", lambda name: None)

    assert hook.verify_python(tmp_path) == sys.executable


def test_verify_python_prefers_venv_without_probing(tmp_path, monkeypatch):
    # A venv is the project's declared environment: a missing dep there is a real
    # provisioning failure to surface, not something to route around via PATH.
    py = tmp_path / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(hook, "_can_verify", lambda exe: pytest.fail("venv must not be probed"))

    assert hook.verify_python(tmp_path) == str(py)


def test_can_verify_detects_a_missing_import(tmp_path):
    # Exercised against real interpreters, so the probe itself is not just a mock.
    hook._can_verify.cache_clear()
    try:
        assert hook._can_verify(sys.executable) is True  # runs pytest right now
        assert hook._can_verify(str(tmp_path / "does-not-exist")) is False
    finally:
        hook._can_verify.cache_clear()


def test_verify_python_used_by_lint_and_test_checks(tmp_path, monkeypatch):
    py = tmp_path / ".venv/Scripts/python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(hook, "REPO_ROOT", tmp_path)
    # The script constants resolve at import against the *real* repo root, so moving
    # REPO_ROOT does not move them, and the script-backed checks now skip when their
    # script is absent. Point each at a real file in the sandbox -- otherwise this
    # asserts nothing about two of the three.
    for attr, rel in (
        ("LINT_ALL", "scripts/lint-all.py"),
        ("CHECK_LOCK_MARKERS", "scripts/clm.py"),
    ):
        script = tmp_path / rel
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("")
        monkeypatch.setattr(hook, attr, script)

    for check in (hook.CHECK_LINT, hook.CHECK_SCRIPT_TESTS, hook.CHECK_LOCKS):
        spec = hook._command_for(check)
        assert spec is not None, f"{check} must be runnable when its script exists"
        argv, _cwd, _artifact = spec
        assert argv[0] == str(py), f"{check} must run under the venv interpreter"


# --- a check whose script this project does not have is a SKIP, not a failure ---
# Returning an argv for a missing script does not skip it: the interpreter exits 2
# with "can't open file", which run_checks reports as a failure with a usage message
# in place of a finding and nothing in the source tree that can fix it. No generated
# project ships check-lock-markers.py, so this fired on every lockfile change.


def test_command_for_skips_checks_whose_script_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "LINT_ALL", tmp_path / "nope-lint-all.py")
    monkeypatch.setattr(hook, "CHECK_LOCK_MARKERS", tmp_path / "nope-check-locks.py")
    assert hook._command_for(hook.CHECK_LINT) is None
    assert hook._command_for(hook.CHECK_LOCKS) is None
    # The infra-free pytest tier needs no project script, so it still runs.
    assert hook._command_for(hook.CHECK_SCRIPT_TESTS) is not None


def test_lint_check_passes_no_secrets_to_the_lint_runner(tmp_path, monkeypatch):
    """`--no-secrets` is contractual: lint-all.py must accept it, so it must be sent.

    A lint runner that does not parse the flag exits 2 from argparse, which is a
    permanent Tier 1 failure on every stop.
    """
    script = tmp_path / "lint-all.py"
    script.write_text("")
    monkeypatch.setattr(hook, "LINT_ALL", script)
    spec = hook._command_for(hook.CHECK_LINT)
    assert spec is not None
    argv, _cwd, artifact = spec
    assert "--changed" in argv and "--no-secrets" in argv
    assert artifact == "logs/lint-errors.log"


def _set_db_enabled(monkeypatch, enabled: bool) -> None:
    """Swap in a Config whose DB tier is on/off, whatever this project's manifest says.

    `Config`/`DbConfig` are frozen, so the flag cannot be poked in place — and it must
    not be, since these tests are vendored and run against every project's real
    manifest. Replacing the whole config keeps them shape-agnostic.
    """
    monkeypatch.setattr(hook, "CFG", replace(hook.CFG, db=replace(hook.CFG.db, enabled=enabled)))


# --- Tier 2b without a DB: the suite still has to run -------------------------
# `run_db_tests` returns [] when `[db] enabled = false`, and it is the only tier that
# consults `host_test_targets`. So a DB-less project used to run lint and the vendored
# hook tests and silently never its own test suite. `run_host_tests` is the seam that
# fixes it; these tests pin both branches without asserting any one project's shape.


def test_run_host_tests_runs_pytest_without_a_db(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, False)
    calls: list[list[str]] = []

    def fake_pytest(targets, repo_root, extra_env=None):
        calls.append(list(targets))
        assert extra_env is None, "a DB-less run must not inject DB env"
        return []

    monkeypatch.setattr(hook, "_pytest_failures", fake_pytest)
    changed = [f"{CFG.tests_dir}test_something.py"]
    assert hook.run_host_tests(changed, {}, tmp_path) == []
    assert calls == [changed], "the changed test file should have been run"


def test_run_host_tests_reports_a_failure_without_a_db(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, False)
    monkeypatch.setattr(
        hook, "_pytest_failures", lambda *a, **k: [(hook.CHECK_TESTS, None, "1 failed")]
    )
    failures = hook.run_host_tests([f"{CFG.tests_dir}test_x.py"], {}, tmp_path)
    assert [name for name, _artifact, _tail in failures] == [hook.CHECK_TESTS]


def test_run_host_tests_is_a_no_op_when_no_test_code_changed(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, False)
    monkeypatch.setattr(hook, "_pytest_failures", lambda *a, **k: pytest.fail("should not run"))
    assert hook.run_host_tests(["README.md", "docs/x.md"], {}, tmp_path) == []


def test_pytest_failures_treats_no_tests_collected_as_a_pass(monkeypatch, tmp_path):
    """pytest exit 5 must not block the stop.

    Targets are *changed files* under tests/, so editing a helper that holds no tests
    of its own (conftest.py, a support module) yields "no tests ran" — a failure the
    agent cannot fix by editing source.
    """

    class _Result:
        returncode = hook.PYTEST_NO_TESTS_COLLECTED
        stdout = "no tests ran in 0.01s"
        stderr = ""

    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _Result())
    assert hook._pytest_failures(["tests/support.py"], tmp_path) == []


def test_pytest_failures_reports_a_real_failure(monkeypatch, tmp_path):
    class _Result:
        returncode = 1
        stdout = "1 failed"
        stderr = ""

    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _Result())
    failures = hook._pytest_failures(["tests/test_x.py"], tmp_path)
    assert [name for name, _artifact, _tail in failures] == [hook.CHECK_TESTS]


def test_run_host_tests_delegates_to_the_db_tier_when_a_db_is_configured(monkeypatch, tmp_path):
    _set_db_enabled(monkeypatch, True)
    seen: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(
        hook, "run_db_tests", lambda paths, env, repo_root: seen.append((paths, repo_root)) or []
    )
    hook.run_host_tests(["app/main.py"], {}, tmp_path)
    assert seen == [(["app/main.py"], tmp_path)], "the DB tier owns infra gating, not this"
