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

Usage:
    python scripts/lint-all.py             # src + tests + scripts
    python scripts/lint-all.py --changed   # ruff over the diff; mypy still full
"""

from __future__ import annotations

import argparse
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


def changed_python_files() -> list[str]:
    """Modified and untracked Python paths that still exist, excluding vendored ones."""
    commands = (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            paths.update(result.stdout.splitlines())
    return sorted(
        path
        for path in paths
        if path.endswith(".py")
        and not path.startswith("scripts/hooks/")
        and (REPO_ROOT / path).is_file()
    )


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
    parser.add_argument("--no-secrets", action="store_true", help="compatibility no-op")
    args = parser.parse_args(argv)

    if args.changed:
        targets = changed_python_files()
        if not targets:
            print("lint-all: no changed Python files; nothing to do")
            ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
            ARTIFACT.write_text("", encoding="utf-8")
            return 0
    else:
        targets = list(DEFAULT_TARGETS)

    print(f"lint-all: {'changed' if args.changed else 'whole repo'}")
    results = [
        run_check("ruff", ["ruff", "check", *targets]),
        run_check("format", ["ruff", "format", "--check", *targets]),
        # Always the configured scope; see the module docstring.
        run_check("mypy", ["mypy", "src"]),
    ]

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
