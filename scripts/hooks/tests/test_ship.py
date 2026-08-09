"""Unit tests for the deterministic mechanics behind /ship."""

from conftest import load_module

ship = load_module("scripts/ship.py")


class _Result:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_only_claude_task_branches_are_shippable():
    assert ship.is_shippable("claude/fix-thing", "main") == (True, "")
    for branch in ("", "main", "feature/x", "carameli-b"):
        ok, _ = ship.is_shippable(branch, "main")
        assert not ok


def test_default_branch_uses_shared_detection(monkeypatch):
    monkeypatch.setattr(ship.tb, "detect_default_branch", lambda git, fallback: "trunk")
    assert ship.default_branch() == "trunk"


def test_tree_clean_ignores_whitespace():
    assert ship.tree_clean(" \n")
    assert not ship.tree_clean(" M app.py\n")


def test_push_retries_transient_failure(monkeypatch):
    results = [_Result(1, stderr="connection timed out"), _Result(0)]
    monkeypatch.setattr(ship, "_git", lambda *args: results.pop(0))
    slept: list[int] = []
    assert ship._push("claude/x", sleep=slept.append)
    assert slept == [2]


def test_push_does_not_retry_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ship,
        "_git",
        lambda *args: calls.append(args) or _Result(1, stderr="non-fast-forward"),
    )
    assert not ship._push("claude/x", sleep=lambda _: None)
    assert len(calls) == 1


def _wire_main(monkeypatch, *, branch="claude/x", clean=True, lint=True, push=True):
    monkeypatch.setattr(ship, "current_branch", lambda: branch)
    monkeypatch.setattr(ship, "default_branch", lambda: "main")
    monkeypatch.setattr(ship, "_porcelain", lambda: "" if clean else " M x.py\n")
    monkeypatch.setattr(ship, "_run_lint", lambda: lint)
    monkeypatch.setattr(ship, "_push", lambda value: push)


def test_preflight_reports_branch_and_base(monkeypatch, capsys):
    _wire_main(monkeypatch)
    assert ship.main(["--preflight"]) == ship.EXIT_OK
    assert "branch=claude/x base=main" in capsys.readouterr().out


def test_push_requires_clean_tree(monkeypatch):
    _wire_main(monkeypatch, clean=False)
    assert ship.main([]) == ship.EXIT_DIRTY_TREE


def test_unknown_arguments_are_rejected(monkeypatch):
    _wire_main(monkeypatch)
    assert ship.main(["--preflight", "--nonsense"]) == ship.EXIT_USAGE
