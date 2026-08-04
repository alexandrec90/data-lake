#!/usr/bin/env python3
"""Run the test suite and write failures to a parseable artifact.

The `test` / `test-changed` contract entrypoint the shared workspace tasks dispatch to
(`devkit_project.ACTIONS["test"]`), and the runner the vendored Stop hook calls before
letting a session end. The agent fixing a failure reads `logs/test-failures.log`, not
the terminal — see `.claude/rules/engineering.md`.

**Everything runs through `uv run`**, not the current interpreter. devkit's template
uses `sys.executable`, which is right for a project whose venv is already active; here
the caller is usually a VS Code task or a hook, launched with the desktop's PATH rather
than this project's `.venv`, and a bare interpreter fails on the first `data_lake`
import. `uv run` resolves the locked environment from `uv.lock` regardless of who
called.

NO DATABASE AND NO NETWORK is a property of this suite, not an accident: connectors are
tested against mocked HTTP clients and an in-memory SQLite session injected per call, so
there is nothing to start first. `.devkit.toml` declares `[db] enabled = false` for the
same reason.

Usage:
    python scripts/run-tests.py             # the whole suite
    python scripts/run-tests.py --changed   # pytest's last-failed subset
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = REPO_ROOT / "logs" / "test-failures.log"

# Per-failure line cap. Holds a first-party traceback plus the assertion without
# letting one deep failure crowd out the rest of the run.
MAX_LINES_PER_FAILURE = 25

# pytest's EXIT_NOTESTSCOLLECTED, which is not a failure of this runner. `stop.py`
# calls this with explicit targets (the changed files under tests/), so editing a
# helper that holds no tests of its own — a conftest, a support module — collects
# nothing. Reporting that as a failure would block the stop with "no tests ran",
# which no source edit can resolve.
PYTEST_NO_TESTS_COLLECTED = 5


def filter_output(raw: str) -> str:
    """Keep the failure sections; drop passing noise and third-party frames. Pure."""
    keep: list[str] = []
    in_failures = False
    for line in raw.splitlines():
        if "=== FAILURES ===" in line or "= FAILURES =" in line:
            in_failures = True
        if "= short test summary info =" in line:
            in_failures = True
        if in_failures:
            # An agent cannot fix a frame inside site-packages, and a long
            # SQLAlchemy or httpx traceback hides the one first-party frame.
            if "site-packages" in line or "/lib/python" in line:
                continue
            keep.append(line)
    return "\n".join(keep).strip()


def cap_failure_blocks(text: str, limit: int = MAX_LINES_PER_FAILURE) -> str:
    """Truncate each `___ test_name ___` block to `limit` lines, noting the cut. Pure."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("_" * 5) and current:
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    out: list[str] = []
    for block in blocks:
        if len(block) > limit:
            out.extend(block[:limit])
            out.append(f"... ({len(block)} lines total, truncated)")
        else:
            out.extend(block)
    return "\n".join(out)


def build_command(changed: bool, extra: list[str]) -> list[str]:
    """The argv to run. Pure — unit-tested, so the uv wrapping cannot silently regress."""
    cmd = ["uv", "run", "python", "-m", "pytest", "--tb=short", "-q"]
    if changed:
        cmd += ["--last-failed", "--last-failed-no-failures", "all"]
    return cmd + [a for a in extra if a]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed", action="store_true", help="run pytest's last-failed subset")
    args, extra = parser.parse_known_args(argv)

    cmd = build_command(args.changed, extra)
    print(f"run-tests: {' '.join(cmd[4:])}")
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    raw = result.stdout + result.stderr

    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    if result.returncode in (0, PYTEST_NO_TESTS_COLLECTED):
        # Clear on pass, so a stale artifact never sends the next agent chasing a
        # failure that is already fixed.
        ARTIFACT.write_text("", encoding="utf-8")
        print(f"run-tests: passed (artifact cleared: {ARTIFACT.relative_to(REPO_ROOT)})")
        return 0

    body = cap_failure_blocks(filter_output(raw))
    # Never leave the agent with nothing: if filtering stripped everything (an
    # unexpected pytest output shape, a collection error), fall back to raw.
    if not body.strip():
        body = raw.strip()
    ARTIFACT.write_text(
        "# source: scripts/run-tests.py\n"
        "# fix: uv run pytest <the failing test id> --tb=long\n" + body + "\n",
        encoding="utf-8",
    )
    print(f"run-tests: FAILED - details in {ARTIFACT.relative_to(REPO_ROOT)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
