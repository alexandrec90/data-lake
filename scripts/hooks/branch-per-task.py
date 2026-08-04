#!/usr/bin/env python3
"""UserPromptSubmit hook: start each new task on a fresh branch off origin/master.

Vibe-coding many small changes in parallel means every task should get its own
short-lived branch cut from the latest master -- otherwise work piles onto
whatever branch happened to be checked out and the branches drift. This hook
handles the "start" half automatically for the PRIMARY checkout: when a prompt
arrives while sitting on `master`, it cuts a new `claude/<slug>` branch.

It fires on two safe triggers (see `task_branch.auto_branch_decision`): sitting
on the default branch (primary checkout), or sitting on the branch `/ship` just
shipped with a clean tree (the worktree case -- `/ship` drops a per-worktree
marker, this hook consumes it). On any other branch, or mid-task, it is a fast
no-op -- it must never cut a branch mid-task. The marker is cleared after an
auto-branch so an empty fresh branch never re-triggers.

Best-effort and always exit 0: a failure here can never block the prompt. The
decision/formatting helpers live in `scripts/task_branch.py` (shared with
`/task`) and are unit-tested in `scripts/hooks/tests/test_task_branch.py`.
"""

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path

# scripts/ on path so the shared, stdlib-only helper imports before the venv.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_branch as tb


def _git(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _current_branch() -> str:
    try:
        result = _git("branch", "--show-current")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _tree_dirty() -> bool:
    try:
        result = _git("status", "--porcelain")
    except (OSError, subprocess.TimeoutExpired):
        return True  # unknown -> treat as dirty (the safe, non-clobbering choice)
    return bool(result.stdout.strip())


def _existing_branches() -> set[str]:
    try:
        result = _git("branch", "--list", "--format=%(refname:short)")
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _marker_path() -> Path | None:
    """Resolve the per-worktree shipped-marker path via `git rev-parse`."""
    try:
        result = _git("rev-parse", "--git-path", tb.SHIPPED_MARKER_NAME)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    return Path(raw) if raw else None


def _read_shipped_marker() -> str:
    """Branch name `/ship` recorded, or '' when there is no marker."""
    path = _marker_path()
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _clear_shipped_marker() -> None:
    path = _marker_path()
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _emit_context(text: str) -> None:
    """Feed a note back into the session as UserPromptSubmit additionalContext."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": text,
                }
            }
        )
    )


def main() -> int:
    raw = ""
    try:
        if sys.stdin is not None and not sys.stdin.isatty():
            raw = sys.stdin.read()
    except (OSError, ValueError):
        raw = ""

    # Cloud/remote (Claude Code on the web / mobile): the platform owns the branch
    # lifecycle. Standing down here keeps a post-`/ship` prompt from cutting a new
    # local branch that diverges from the one the mobile app tracks. See
    # tb.platform_manages_branch and the guard in .claude/hooks/session-start.sh.
    if tb.platform_manages_branch(os.environ):
        return 0

    current = _current_branch()
    shipped = _read_shipped_marker()
    dirty = _tree_dirty()
    # Resolve the repo's default branch (main/master/...) rather than assuming one.
    default_branch = tb.detect_default_branch(_git)
    should, base = tb.auto_branch_decision(current, shipped, dirty, default_branch)
    if not should:
        return 0  # mid-task, or on an unshipped feature branch, or detached -- no-op.

    # Starting new work: refresh origin so the cut is from latest.
    # Offline is fine -- fall back to whatever origin/<default> we already have.
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        _git("fetch", "--prune", "origin", default_branch, timeout=60.0)

    name = tb.branch_name(tb.slug_from_prompt(tb.parse_prompt(raw)), _existing_branches())
    argv = tb.checkout_argv(name, base)
    try:
        result = _git(*argv)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if result.returncode != 0:
        return 0  # never block the prompt on a git failure.

    # Consume the marker so the fresh (empty) branch never re-triggers.
    _clear_shipped_marker()

    if base:
        origin = f" (the shipped '{shipped}' was left behind)" if shipped == current else ""
        _emit_context(f"Started task on fresh branch '{name}' (cut from {base}){origin}.")
    else:
        _emit_context(
            f"Started task on fresh branch '{name}' carrying your uncommitted "
            f"changes (tree was dirty, so it was not reset onto origin/{default_branch})."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
