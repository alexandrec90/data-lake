#!/usr/bin/env python3
"""PreToolUse hook: cut this task's branch at the first edit, not at the prompt.

The other half of `branch-per-task.py`. That hook records the slug the task should
be named after; this one performs the checkout, on the first Edit/Write/patch of
the turn, when the two questions that matter are finally answerable:

  - **Is this actually new work?** A read-only turn never reaches a mutating tool,
    so it never cuts a branch. Cutting at the prompt named branches after questions.
  - **Where are we?** "Fix PR #42, it has merge conflicts" starts on the default
    branch but the agent checks the PR's branch out before touching anything -- so
    by the first edit `auto_branch_decision` sees a feature branch and correctly
    declines. Cutting at the prompt put that fix on a branch the PR never sees.

Fires on every edit, so the no-op path is deliberately one `git branch
--show-current` and out: after the first cut the session is on a feature branch and
declines there. That is the same cost as the ruff invocations `lint-fix.py` already
spends per edit.

Bash is *not* matched. It would branch on read-only commands, and the mutating ones
worth catching (`git commit`, a generated file) are preceded by an edit in practice.
A task that only ever shells out therefore stays on the default branch -- the
tradeoff is deliberate: a spurious branch is noise on every turn, a missed one is
noise on rare turns and visible in `git status`.

Best-effort and always exit 0: this hook must never block a tool call. Exit 2 here
would refuse the edit, which is a far worse failure than not branching.

Pure helpers (`parse_hook_input`, `is_mutating_tool`) are unit-tested in
`scripts/hooks/tests/test_branch_on_write.py`; the decision logic they feed lives in
`scripts/task_branch.py`.
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

# The tools whose use means "work has started". Mirrors `lint-fix.py`'s ALLOWED_TOOLS
# plus NotebookEdit: both hooks are answering "did the agent just change a file?".
MUTATING_TOOLS = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "create_file"}
)


def _git(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run git, never raising -- a failed CompletedProcess stands in for an error.

    The injected-git helpers in `task_branch` expect a CompletedProcess back, and a
    hook that must not block has nothing useful to do with an exception anyway.
    """
    try:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(list(args), 1, "", "")


def parse_hook_input(raw: str) -> dict | None:
    """Parse raw stdin into a dict, or None when absent/malformed."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def is_mutating_tool(hook_input: dict | None) -> bool:
    """True when the payload names a tool that changes a file.

    Tolerates snake_case and camelCase keys, as `lint-fix.py` does -- Codex-style
    harnesses send `toolName`. An absent tool name is treated as *not* mutating: the
    matcher in settings.json has already selected the event, and guessing here would
    make an unrecognised payload shape cut branches on read-only turns.
    """
    if not hook_input:
        return False
    tool = hook_input.get("tool_name") or hook_input.get("toolName") or ""
    return tool in MUTATING_TOOLS


def _current_branch() -> str:
    result = _git("branch", "--show-current")
    return result.stdout.strip() if result.returncode == 0 else ""


def _tree_dirty() -> bool:
    result = _git("status", "--porcelain")
    if result.returncode != 0:
        return True  # unknown -> treat as dirty (the safe, non-clobbering choice)
    return bool(result.stdout.strip())


def _existing_branches() -> set[str]:
    result = _git("branch", "--list", "--format=%(refname:short)")
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def take_intent() -> str:
    """Read and consume the slug `branch-per-task.py` recorded for this task.

    Consumed (not just read) so a later accidental visit to the default branch cannot
    name a new branch after a task three prompts ago. Returns '' when there is no
    marker -- the caller then falls back to `slugify`'s own default.
    """
    path = tb.worktree_file(_git, tb.TASK_INTENT_MARKER_NAME)
    if path is None or not path.exists():
        return ""
    slug = ""
    with contextlib.suppress(OSError):
        slug = path.read_text(encoding="utf-8").strip()
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    return slug


def _emit_context(text: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
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

    if not is_mutating_tool(parse_hook_input(raw)):
        return 0
    # Cloud/remote: the platform owns the branch lifecycle (see branch-per-task.py).
    if tb.platform_manages_branch(os.environ):
        return 0

    # The fast no-op path -- one git call for every edit after the first.
    current = _current_branch()
    default_branch = tb.detect_default_branch(_git)
    should, base = tb.auto_branch_decision(current, _tree_dirty(), default_branch)
    if not should:
        return 0

    # Starting new work: refresh origin so the cut is from latest. Offline is fine --
    # fall back to whatever origin/<default> we already have.
    _git("fetch", "--prune", "origin", default_branch, timeout=60.0)

    slug = tb.slugify(take_intent())
    name = tb.branch_name(slug, _existing_branches())
    result = _git(*tb.checkout_argv(name, base))
    if result.returncode != 0:
        return 0  # never block the edit on a git failure.

    if base:
        _emit_context(f"Started task on fresh branch '{name}' (cut from {base}).")
    else:
        _emit_context(
            f"Started task on fresh branch '{name}' carrying your uncommitted "
            f"changes (tree was dirty, so it was not reset onto origin/{default_branch})."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
