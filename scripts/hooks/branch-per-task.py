#!/usr/bin/env python3
"""UserPromptSubmit hook: record what to name this task's branch, and advise.

This hook no longer cuts a branch. It has the prompt text and nothing else, which
is the wrong basis for a checkout:

  - "What does stop.py do?" asked from the default branch cut a branch named after
    the question, for a turn that never wrote a file.
  - "Fix PR #42, it has merge conflicts" cut `claude/fix-pr-42-...` off the default
    branch, when the work belongs on the PR's own branch -- so the agent had to
    check that branch out anyway and left a stray empty one behind.

So it writes the slug to a per-worktree marker and stops. `branch-on-write.py`
consumes the marker at the first mutating tool call, when "is this actually new
work, and where are we?" is answerable. See `task_branch.auto_branch_decision`.

The one thing it still reports on is a prompt arriving on a branch `/ship` already
shipped (`task_branch.spent_branch_notice`): that state is both "the branch is
spent" and "you are about to be asked to fix the PR", and only the prompt tells
them apart -- so the note goes to the model rather than the decision being guessed
here.

Best-effort and always exit 0: a failure here can never block the prompt. The
decision/formatting helpers live in `scripts/task_branch.py` (shared with
`/ship`) and are unit-tested in `scripts/hooks/tests/test_task_branch.py`.
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


def _git_quiet(*args: str) -> subprocess.CompletedProcess[str]:
    """`_git` that never raises -- returns a failed CompletedProcess instead.

    `worktree_file` and `detect_default_branch` take an injected git callable and
    expect a CompletedProcess back; handing them one that can raise moves the
    error handling to every call site.
    """
    try:
        return _git(*args)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(list(args), 1, "", "")


def _current_branch() -> str:
    result = _git_quiet("branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def _tree_dirty() -> bool:
    result = _git_quiet("status", "--porcelain")
    if result.returncode != 0:
        return True  # unknown -> treat as dirty (the safe, non-clobbering choice)
    return bool(result.stdout.strip())


def _existing_branches() -> set[str]:
    result = _git_quiet("branch", "--list", "--format=%(refname:short)")
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _read_marker(name: str) -> str:
    """Contents of a per-worktree marker, or '' when there is none."""
    path = tb.worktree_file(_git_quiet, name)
    if path is None or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def write_intent(slug: str) -> bool:
    """Record `slug` for `branch-on-write.py` to name its cut after. Best-effort."""
    path = tb.worktree_file(_git_quiet, tb.TASK_INTENT_MARKER_NAME)
    if path is None:
        return False
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(slug, encoding="utf-8")
        return True
    return False


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
    # lifecycle. Standing down here keeps the harness from competing with the branch
    # the mobile app tracks. See tb.platform_manages_branch and the guard in
    # .claude/hooks/session-start.sh.
    if tb.platform_manages_branch(os.environ):
        return 0

    slug = tb.slug_from_prompt(tb.parse_prompt(raw))
    write_intent(slug)

    # The spent-branch case is the only one worth a word to the model.
    shipped = _read_marker(tb.SHIPPED_MARKER_NAME)
    if not shipped:
        return 0
    current = _current_branch()
    default_branch = tb.detect_default_branch(_git_quiet)
    suggested = tb.branch_name(slug, _existing_branches())
    notice = tb.spent_branch_notice(current, shipped, _tree_dirty(), suggested, default_branch)
    if notice:
        _emit_context(notice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
