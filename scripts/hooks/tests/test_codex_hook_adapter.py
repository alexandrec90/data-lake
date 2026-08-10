"""Tests for the Codex command-hook compatibility adapter."""

import json
import subprocess

from conftest import load_module

hook = load_module("scripts/hooks/codex-hook-adapter.py")


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_pretool_failure_becomes_json_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(42, stdout="blocked by project policy"),
    )

    code, stdout, stderr = hook.run_hook(
        "PreToolUse", ["python3", "guard.py"], "{}", repo_root=tmp_path
    )

    assert code == 0
    assert stderr == ""
    payload = json.loads(stdout)
    specific = payload["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert specific["permissionDecisionReason"] == "blocked by project policy"


def test_stop_failure_becomes_continuation_decision(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(2, stderr="lint failed"),
    )

    code, stdout, stderr = hook.run_hook("Stop", ["python3", "stop.py"], "{}", repo_root=tmp_path)

    assert code == 0
    assert stderr == ""
    assert json.loads(stdout) == {"decision": "block", "reason": "lint failed"}


def test_posttool_failure_becomes_block_feedback(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(2, stderr="formatting failed"),
    )

    _, stdout, _ = hook.run_hook(
        "PostToolUse", ["python3", "lint-fix.py"], "{}", repo_root=tmp_path
    )

    assert json.loads(stdout) == {
        "decision": "block",
        "reason": "formatting failed",
    }


def test_permission_failure_uses_permission_request_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(42, stderr="approval denied"),
    )

    _, stdout, _ = hook.run_hook(
        "PermissionRequest", ["python3", "policy.py"], "{}", repo_root=tmp_path
    )

    assert json.loads(stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": "approval denied",
            },
        }
    }


def test_compaction_failure_uses_common_stop_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(1, stderr="compact guard failed"),
    )

    _, stdout, _ = hook.run_hook("PreCompact", ["python3", "guard.py"], "{}", repo_root=tmp_path)

    assert json.loads(stdout) == {
        "continue": False,
        "stopReason": "compact guard failed",
        "systemMessage": "compact guard failed",
    }


def test_success_preserves_output_and_sets_project_dir(monkeypatch, tmp_path):
    captured = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return _completed(0, stdout='{"continue":true}\n', stderr="note\n")

    monkeypatch.setattr(hook.subprocess, "run", fake_run)

    code, stdout, stderr = hook.run_hook(
        "UserPromptSubmit", ["python3", "branch.py"], '{"prompt":"x"}', repo_root=tmp_path
    )

    assert (code, stdout, stderr) == (0, '{"continue":true}\n', "")
    assert captured["cwd"] == tmp_path
    assert captured["env"]["CLAUDE_PROJECT_DIR"] == str(tmp_path)
    assert captured["env"]["PYTHONUTF8"] == "1"
    assert captured["input"] == '{"prompt":"x"}'


def test_session_start_success_suppresses_operational_output(monkeypatch, tmp_path):
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            0,
            stdout="[session-start] branch sync skipped\n",
            stderr="informational detail\n",
        ),
    )

    code, stdout, stderr = hook.run_hook(
        "SessionStart", ["python3", "session-start.py"], "{}", repo_root=tmp_path
    )

    assert (code, stdout, stderr) == (0, "", "")


def test_missing_command_is_reported_without_failing_adapter():
    code, stdout, stderr = hook.run_hook("SessionStart", [], "{}")
    assert code == 0
    assert stderr == ""
    assert "no command" in json.loads(stdout)["systemMessage"].lower()


def test_failure_reason_is_byte_capped_with_head_and_tail():
    text = "A" * 5000 + "TAIL"
    capped = hook.cap_reason(text, max_bytes=100)
    assert len(capped.encode()) <= 100
    assert capped.startswith("A")
    assert capped.endswith("TAIL")
    assert "truncated" in capped


def test_parse_args_strips_separator():
    args = hook.parse_args(["--event", "Stop", "--", "python3", "stop.py"])
    assert args.event == "Stop"
    assert args.command == ["python3", "stop.py"]
