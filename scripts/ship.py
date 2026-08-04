#!/usr/bin/env python3
"""Mechanical checks, lint, push, and shipped-marker handling for /ship."""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_branch as tb

REPO_ROOT = Path(__file__).resolve().parents[1]
LINT_ALL = REPO_ROOT / "scripts" / "lint-all.py"
TASK_BRANCH_PREFIX = "claude/"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_SHIPPABLE = 3
EXIT_DIRTY_TREE = 4
EXIT_LINT_FAILED = 5
EXIT_PUSH_FAILED = 6


def is_shippable(branch: str, default: str, prefix: str = TASK_BRANCH_PREFIX) -> tuple[bool, str]:
    """Return whether branch is an isolated task branch suitable for a PR."""
    if not branch:
        return False, "HEAD is detached; check out a task branch before shipping."
    if branch == default:
        return False, f"'{default}' is the default branch; ship from a {prefix} task branch."
    if not branch.startswith(prefix):
        return False, f"'{branch}' is not a {prefix} task branch; refusing to ship it."
    return True, ""


def tree_clean(porcelain: str) -> bool:
    return not porcelain.strip()


def backoff_delays() -> list[int]:
    return [2, 4, 8, 16]


def _git(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=capture,
        text=True,
        check=False,
    )


def current_branch() -> str:
    result = _git("branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def default_branch() -> str:
    return tb.detect_default_branch(_git, fallback="main")


def _porcelain() -> str:
    result = _git("status", "--porcelain")
    return result.stdout if result.returncode == 0 else ""


def write_shipped_marker(branch: str) -> None:
    """Record a successfully PR'd branch for the next-prompt branch hook."""
    result = _git("rev-parse", "--git-path", tb.SHIPPED_MARKER_NAME)
    if result.returncode != 0 or not result.stdout.strip():
        return
    path = REPO_ROOT / result.stdout.strip()
    with contextlib.suppress(OSError):
        path.write_text(branch + "\n", encoding="utf-8")


def _run_lint() -> bool:
    if not LINT_ALL.is_file():
        print(f"ship: required lint runner is missing: {LINT_ALL}", file=sys.stderr)
        return False
    try:
        result = subprocess.run(
            [sys.executable, str(LINT_ALL), "--changed"],
            cwd=REPO_ROOT,
            check=False,
        )
    except OSError as exc:
        print(f"ship: could not run lint gate: {exc}", file=sys.stderr)
        return False
    return result.returncode == 0


def _push(branch: str, sleep=time.sleep) -> bool:
    """Push the task branch, retrying only recognizably transient failures."""
    delays = backoff_delays()
    for attempt in range(len(delays) + 1):
        result = _git("push", "-u", "origin", branch)
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").lower()
        transient = any(
            marker in stderr
            for marker in ("could not resolve", "timed out", "connection", "network")
        )
        if not transient or attempt == len(delays):
            if result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)
            return False
        sleep(delays[attempt])
    return False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv not in ([], ["--preflight"], ["--mark-shipped"]):
        print("usage: ship.py [--preflight | --mark-shipped]", file=sys.stderr)
        return EXIT_USAGE

    branch = current_branch()
    base = default_branch()
    ok, reason = is_shippable(branch, base)
    if not ok:
        print(f"ship: {reason}", file=sys.stderr)
        return EXIT_NOT_SHIPPABLE

    if argv == ["--preflight"]:
        print(f"ship: branch={branch} base={base}")
        return EXIT_OK

    if argv == ["--mark-shipped"]:
        write_shipped_marker(branch)
        print(f"ship: marked '{branch}' shipped; the next prompt starts a fresh task branch.")
        return EXIT_OK

    if not tree_clean(_porcelain()):
        print("ship: working tree is dirty; commit the intended changes first.", file=sys.stderr)
        return EXIT_DIRTY_TREE
    if not _run_lint():
        print("ship: changed-scope lint failed; see logs/lint-errors.log.", file=sys.stderr)
        return EXIT_LINT_FAILED
    if not _push(branch):
        print("ship: push failed after retries.", file=sys.stderr)
        return EXIT_PUSH_FAILED

    print(f"ship: pushed branch={branch} base={base}; open or reuse its PR before marking shipped.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
