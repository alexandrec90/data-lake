"""Unit tests for the deferred branch cut (`scripts/hooks/branch-on-write.py`).

**This file is vendored into every consuming project**, so nothing here may assert a
value specific to one project -- no repo's default branch, no repo's paths.

The behaviour worth pinning is *when* the hook declines, because every one of those
cases used to be a spurious branch: a read-only turn, a turn already on a feature
branch, a cloud session. The cut itself is `task_branch.checkout_argv`, tested there.
"""

import json
import subprocess

import pytest
from conftest import load_module

hook = load_module("scripts/hooks/branch-on-write.py")
tb = load_module("scripts/task_branch.py")


def payload(tool_name=None, **extra):
    body = dict(extra)
    if tool_name is not None:
        body["tool_name"] = tool_name
    return json.dumps(body)


class TestParseHookInput:
    def test_parses_an_object(self):
        assert hook.parse_hook_input('{"tool_name": "Edit"}') == {"tool_name": "Edit"}

    def test_none_for_empty_or_malformed(self):
        assert hook.parse_hook_input("") is None
        assert hook.parse_hook_input("not json") is None

    def test_none_for_a_non_object(self):
        assert hook.parse_hook_input("[1, 2]") is None


class TestIsMutatingTool:
    @pytest.mark.parametrize(
        "tool", ["Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "create_file"]
    )
    def test_true_for_every_mutating_tool(self, tool):
        assert hook.is_mutating_tool({"tool_name": tool}) is True

    def test_accepts_camel_case_payloads(self):
        """Codex-style harnesses send `toolName`; `lint-fix.py` tolerates both too."""
        assert hook.is_mutating_tool({"toolName": "Write"}) is True

    @pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "Bash", "WebFetch", "Task"])
    def test_false_for_read_only_tools(self, tool):
        """The whole reason the cut moved off UserPromptSubmit.

        A turn that only reads must never cut a branch -- that is what named branches
        after questions like "what does stop.py do?".
        """
        assert hook.is_mutating_tool({"tool_name": tool}) is False

    def test_false_for_a_missing_tool_name(self):
        # Guessing "mutating" from an unrecognised payload shape would reintroduce
        # branch-cutting on read-only turns, so absence declines.
        assert hook.is_mutating_tool({}) is False
        assert hook.is_mutating_tool(None) is False

    def test_bash_is_deliberately_excluded(self):
        assert "Bash" not in hook.MUTATING_TOOLS


class TestTakeIntent:
    def test_reads_and_consumes_the_marker(self, tmp_path, monkeypatch):
        marker = tmp_path / tb.TASK_INTENT_MARKER_NAME
        marker.write_text("add-sms-retry", encoding="utf-8")
        monkeypatch.setattr(tb, "worktree_file", lambda git, name: marker)

        assert hook.take_intent() == "add-sms-retry"
        # Consumed: a later accidental visit to the default branch must not be named
        # after a task three prompts ago.
        assert not marker.exists()
        assert hook.take_intent() == ""

    def test_empty_when_there_is_no_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "worktree_file", lambda git, name: tmp_path / "absent")
        assert hook.take_intent() == ""

    def test_empty_when_git_cannot_resolve_a_path(self, monkeypatch):
        monkeypatch.setattr(tb, "worktree_file", lambda git, name: None)
        assert hook.take_intent() == ""

    def test_a_missing_intent_still_yields_a_usable_slug(self, tmp_path, monkeypatch):
        """The fallback path: no marker must not mean a branch named ''.

        Must stub `worktree_file` like every sibling here. Unstubbed, this resolved the
        *live* marker in the developer's own worktree -- so it asserted the fallback
        while reading a real slug (green only when no task was in flight), and
        `take_intent` consumed the marker as a side effect, leaving that session's next
        cut named `task-<date>`.
        """
        monkeypatch.setattr(tb, "worktree_file", lambda git, name: tmp_path / "absent")
        assert tb.slugify(hook.take_intent() or "") == "task"


class TestGitNeverRaises:
    def test_a_spawn_failure_becomes_a_failed_completed_process(self, monkeypatch):
        """A hook that must not block a tool call has nothing to do with an exception."""

        def boom(*a, **k):
            raise OSError("git missing")

        monkeypatch.setattr(hook.subprocess, "run", boom)
        result = hook._git("status")
        assert isinstance(result, subprocess.CompletedProcess)
        assert result.returncode == 1

    def test_a_timeout_becomes_a_failed_completed_process(self, monkeypatch):
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(hook.subprocess, "run", timeout)
        assert hook._git("status").returncode == 1


class TestMainDeclines:
    """`main` must exit 0 and touch no git state on every declining path."""

    def _forbid_git(self, monkeypatch):
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(
            hook,
            "_git",
            lambda *args, **kw: calls.append(args) or subprocess.CompletedProcess([], 1),
        )
        return calls

    def test_read_only_tool_is_a_no_op(self, monkeypatch):
        calls = self._forbid_git(monkeypatch)
        monkeypatch.setattr(hook.sys, "stdin", _Stdin(payload("Read")))
        assert hook.main() == 0
        assert calls == [], "a read-only tool must not even ask git anything"

    def test_cloud_session_stands_down(self, monkeypatch):
        calls = self._forbid_git(monkeypatch)
        monkeypatch.setattr(hook.sys, "stdin", _Stdin(payload("Edit")))
        monkeypatch.setitem(hook.os.environ, "CLAUDE_CODE_REMOTE", "true")
        assert hook.main() == 0
        assert calls == [], "the platform owns the branch lifecycle remotely"

    def test_never_returns_a_blocking_exit_code(self, monkeypatch):
        """Exit 2 here would refuse the edit -- far worse than not branching."""
        monkeypatch.setattr(hook.sys, "stdin", _Stdin(payload("Edit")))
        monkeypatch.setattr(hook, "_current_branch", lambda: "claude/x-0801")
        monkeypatch.setattr(hook, "_tree_dirty", lambda: False)
        monkeypatch.setattr(
            hook, "_git", lambda *a, **k: subprocess.CompletedProcess([], 0, "", "")
        )
        assert hook.main() == 0


class TestMainCuts:
    def test_cuts_from_the_default_branch_and_names_it_after_the_intent(self, monkeypatch):
        ran: list[tuple[str, ...]] = []

        def fake_git(*args, **kw):
            ran.append(args)
            if args[:2] == ("branch", "--show-current"):
                return subprocess.CompletedProcess([], 0, "trunk\n", "")
            if args[0] == "status":
                return subprocess.CompletedProcess([], 0, "", "")
            if args[0] == "branch":
                return subprocess.CompletedProcess([], 0, "trunk\n", "")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(hook.sys, "stdin", _Stdin(payload("Edit")))
        monkeypatch.setattr(hook, "_git", fake_git)
        monkeypatch.setattr(hook.tb, "detect_default_branch", lambda git: "trunk")
        monkeypatch.setattr(hook, "take_intent", lambda: "add-sms-retry")
        monkeypatch.delitem(hook.os.environ, "CLAUDE_CODE_REMOTE", raising=False)

        assert hook.main() == 0
        checkouts = [args for args in ran if args and args[0] == "checkout"]
        assert len(checkouts) == 1, f"expected exactly one checkout, got {ran}"
        argv = checkouts[0]
        assert "--no-track" in argv
        assert any("add-sms-retry" in part for part in argv)
        assert argv[-1] == "origin/trunk"

    def test_a_failed_checkout_is_swallowed(self, monkeypatch):
        def fake_git(*args, **kw):
            if args[0] == "checkout":
                return subprocess.CompletedProcess([], 128, "", "fatal: whatever")
            if args[:2] == ("branch", "--show-current"):
                return subprocess.CompletedProcess([], 0, "main\n", "")
            return subprocess.CompletedProcess([], 0, "", "")

        monkeypatch.setattr(hook.sys, "stdin", _Stdin(payload("Write")))
        monkeypatch.setattr(hook, "_git", fake_git)
        monkeypatch.setattr(hook.tb, "detect_default_branch", lambda git: "main")
        monkeypatch.setattr(hook, "take_intent", lambda: "x")
        monkeypatch.delitem(hook.os.environ, "CLAUDE_CODE_REMOTE", raising=False)

        assert hook.main() == 0


class _Stdin:
    """Minimal stdin stand-in: non-tty, returns `data` once."""

    def __init__(self, data: str):
        self._data = data

    def isatty(self):
        return False

    def read(self):
        return self._data
