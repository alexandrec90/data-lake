#!/usr/bin/env python3
"""Run ruff, the format check, and mypy; write findings to a parseable artifact.

The `lint` / `lint-changed` contract entrypoint the shared workspace tasks dispatch to
(`devkit_project.ACTIONS["lint"]`), and what the vendored Stop hook calls. Findings go
to `logs/lint-errors.log` — read the artifact, not the terminal.

Everything runs through `uv run`, for the reason spelled out in `run-tests.py`: the
caller is usually a VS Code task or a hook, launched with the desktop's PATH rather than
this project's `.venv`.

`--changed` narrows ruff to the working-tree diff, but **never narrows mypy**. A partial
type check is worse than none: imports pull the package graph back in anyway, so a
subset run reports errors from files it was not asked about while missing the ones it
was — and this package is nothing but a graph of Protocols and connectors that resolve
against each other.

A narrowed run (`--changed`, `--paths`) also runs the commit stage's `detect-secrets`
hook over every file it names, Python or not, unless `--no-secrets` is given.

Usage:
    python scripts/lint-all.py             # src + tests + scripts
    python scripts/lint-all.py --changed   # ruff over the diff; mypy still full
    python scripts/lint-all.py --paths a.py b.md  # the same, naming the files (/ship)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "lint-errors.log"

# `scripts/hooks/` is deliberately absent: it is the VENDORED tier, byte-compared
# against devkit by `sync-devkit.py --check`. Formatting it here would rewrite files
# devkit owns and report as drift the moment anything ran, and the drift would look
# like this project's fault. devkit lints those files in its own repo.
DEFAULT_TARGETS = ("src", "tests")

# The commit-stage hook a narrowed run also runs, through pre-commit so its `exclude` and
# the `# pragma: allowlist secret` convention are the commit's own. Without it every check
# a session runs before `/ship` passed a hard-coded key that the commit then refused, a
# whole dispatch later. `pre-commit run --all-files` does not stand in for this: it reads
# only what git tracks, and the file that refuses is usually new. Not named `SECRETS_*`:
# the scanner reads any assignment to a name holding that word as a keyword secret.
SCAN_HOOK_ID = "detect-secrets"
PRECOMMIT_CONFIG = ".pre-commit-config.yaml"


def changed_paths() -> list[str]:
    """Modified and untracked paths that still exist, of every type."""
    commands = (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            paths.update(result.stdout.splitlines())
    return sorted(path for path in paths if (REPO_ROOT / path).is_file())


def explicit_paths(paths: list[str]) -> list[str]:
    """Exactly the paths given, normalised, minus any that no longer exist: `/ship` names
    its branch diff, and ruff treats a deleted path as a usage error."""
    return sorted({p.replace("\\", "/") for p in paths if (REPO_ROOT / p).is_file()})


def python_targets(paths: list[str]) -> list[str]:
    """The Python files among `paths`, excluding vendored ones."""
    return [p for p in paths if p.endswith(".py") and not p.startswith("scripts/hooks/")]


def secrets_targets(selected: list[str], skip: bool) -> list[str]:
    """The files the secrets pass scans: every selected one, of any type, or none.

    None when `skip` (`--no-secrets`) or when this repo's pre-commit config does not
    declare the hook: a repo without it has no secrets pass to mirror, and pre-commit
    would fail the run naming a missing hook id. Narrowed runs only: a whole-repo run is
    CI's, where pre-commit runs every hook.
    """
    config = REPO_ROOT / PRECOMMIT_CONFIG
    if skip or not selected or not config.is_file():
        return []
    declared = re.search(rf"id:\s*{SCAN_HOOK_ID}\s*$", config.read_text(encoding="utf-8"), re.M)
    return selected if declared else []


def build_command(module_args: list[str]) -> list[str]:
    """The argv for one check. Pure — unit-tested."""
    return ["uv", "run", "python", "-m", *module_args]


def run_check(name: str, module_args: list[str]) -> tuple[str, int, str]:
    """Run one check, returning (name, returncode, combined output)."""
    result = subprocess.run(
        build_command(module_args), cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return name, result.returncode, (result.stdout + result.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="ruff over changed files only")
    # `/ship` passes the branch's diff here: it insists on a clean tree, where `--changed`
    # is empty by construction, and falls back to it when `--help` names no `--paths`.
    parser.add_argument(
        "--paths",
        nargs="+",
        metavar="FILE",
        default=[],
        help="lint exactly these files, scoped as --changed is (used by /ship)",
    )
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help="skip the detect-secrets pass a --changed or --paths run makes",
    )
    args = parser.parse_args(argv)

    scoped = args.changed or bool(args.paths)
    selected: list[str] = []
    if scoped:
        selected = explicit_paths(args.paths) if args.paths else changed_paths()
    secrets = secrets_targets(selected, args.no_secrets)
    targets = python_targets(selected) if scoped else list(DEFAULT_TARGETS)
    if scoped and not (targets or secrets):
        print("lint-all: no changed files this run lints; nothing to do")
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text("", encoding="utf-8")
        return 0

    print(f"lint-all: {'changed' if scoped else 'whole repo'}")
    results = []
    if targets:
        results += [
            run_check("ruff", ["ruff", "check", *targets]),
            run_check("format", ["ruff", "format", "--check", *targets]),
            # Always the configured scope; see the module docstring.
            run_check("mypy", ["mypy", "src"]),
        ]
    if secrets:
        results.append(
            run_check(SCAN_HOOK_ID, ["pre_commit", "run", SCAN_HOOK_ID, "--files", *secrets])
        )

    sections = []
    for name, code, output in results:
        print(f"  {name}: {'ok' if code == 0 else 'FAILED'}")
        if code != 0:
            sections.append(f"=== {name} ===\n{output}")

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    if not sections:
        # Written empty on success too — a stale artifact misleads the next agent.
        ARTIFACT.write_text("", encoding="utf-8")
        print(f"\nlint-all: clean (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
        return 0

    ARTIFACT.write_text(
        "# source: scripts/lint-all.py\n"
        "# fix: uv run ruff check --fix src tests && uv run ruff format src tests\n"
        + "\n\n".join(sections)
        + "\n",
        encoding="utf-8",
    )
    print(f"\nlint-all: FAILED - details in {ARTIFACT.relative_to(REPO_ROOT)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
