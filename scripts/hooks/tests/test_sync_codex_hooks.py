"""Unit tests for the .codex/hooks.json generator (from .claude/settings.json)."""

import json

import pytest
from conftest import load_module

hook = load_module("scripts/sync-codex-hooks.py")


class TestRewriteCommand:
    def test_resolves_project_dir_from_git_root(self):
        assert (
            hook.rewrite_command('python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/example-hook.py"')
            == 'python3 "$(git rev-parse --show-toplevel)/scripts/hooks/example-hook.py"'
        )

    def test_keeps_claude_subpath_after_prefix(self):
        # The .claude/... portion is a real path Codex reads directly.
        assert (
            hook.rewrite_command('python3 "${CLAUDE_PROJECT_DIR:-.}/.claude/skills/example/x.py"')
            == 'python3 "$(git rev-parse --show-toplevel)/.claude/skills/example/x.py"'
        )

    def test_command_without_prefix_is_unchanged(self):
        # Inline echo retros carry no project-dir path.
        cmd = "echo '{\"hookSpecificOutput\": {}}'"
        assert hook.rewrite_command(cmd) == cmd

    def test_rewrites_every_occurrence(self):
        cmd = "${CLAUDE_PROJECT_DIR:-.}/a && ${CLAUDE_PROJECT_DIR:-.}/b"
        root = hook.CODEX_ROOT_EXPR
        assert hook.rewrite_command(cmd) == f"{root}/a && {root}/b"


class TestWrapCommand:
    def test_wraps_python_handler_with_event_adapter(self):
        result = hook.wrap_command(
            "PreToolUse", 'python3 "${CLAUDE_PROJECT_DIR:-.}/scripts/hooks/example-hook.py"'
        )
        assert "codex-hook-adapter.py" in result
        assert "--event PreToolUse -- python3" in result
        assert "$(git rev-parse --show-toplevel)/scripts/hooks/example-hook.py" in result

    def test_session_start_bash_handler_uses_cross_platform_bridge(self):
        result = hook.wrap_command(
            "SessionStart", 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/session-start.sh"'
        )
        assert "--event SessionStart -- python3" in result
        assert "codex-session-start.py" in result
        assert ".claude/hooks/session-start.sh" not in result

    def test_leaves_shell_builtin_unwrapped(self):
        command = 'echo \'{"systemMessage":"x"}\''
        assert hook.wrap_command("PostToolUse", command) == command


class TestToCodexHooks:
    def test_wraps_in_hooks_key(self):
        assert hook.to_codex_hooks({"hooks": {}}) == {"hooks": {}}

    def test_missing_hooks_yields_empty(self):
        assert hook.to_codex_hooks({"env": {"A": "1"}}) == {"hooks": {}}

    def test_carries_supported_events_and_wraps_commands(self):
        claude = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": 'bash "${CLAUDE_PROJECT_DIR:-.}/x.sh"'}
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {
                                "type": "command",
                                "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/p.py"',
                            }
                        ],
                    }
                ],
            }
        }
        result = hook.to_codex_hooks(claude)
        assert set(result["hooks"]) == {"SessionStart", "PreToolUse"}
        session = result["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        assert session.startswith(hook.CODEX_ADAPTER)
        assert session.endswith(
            '--event SessionStart -- bash "$(git rev-parse --show-toplevel)/x.sh"'
        )
        # The matcher passes through untouched.
        assert result["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
        pretool = result["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        assert pretool.startswith(hook.CODEX_ADAPTER)
        assert pretool.endswith(
            '--event PreToolUse -- python3 "$(git rev-parse --show-toplevel)/p.py"'
        )

    def test_preserves_hook_entry_fields(self):
        claude = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "run"}]}]}}
        entry = hook.to_codex_hooks(claude)["hooks"]["Stop"][0]["hooks"][0]
        assert entry == {"type": "command", "command": "run"}

    def test_drops_unsupported_event(self):
        claude = {
            "hooks": {
                "PostToolUseFailure": [
                    {"matcher": ".*", "hooks": [{"type": "command", "command": "echo nope"}]}
                ]
            }
        }
        assert hook.to_codex_hooks(claude) == {"hooks": {}}

    def test_rejects_unclassified_event(self):
        claude = {"hooks": {"FutureHookEvent": []}}

        with pytest.raises(ValueError, match=r"Unclassified Claude hook event.*FutureHookEvent"):
            hook.to_codex_hooks(claude)

    def test_drops_skill_tool_matcher_but_keeps_edit_matcher(self):
        claude = {
            "hooks": {
                "PostToolUse": [
                    {"matcher": "^Skill$", "hooks": [{"type": "command", "command": "echo nope"}]},
                    {
                        "matcher": "^apply_patch$",
                        "hooks": [{"type": "command", "command": "python3 lint.py"}],
                    },
                ]
            }
        }
        groups = hook.to_codex_hooks(claude)["hooks"]["PostToolUse"]
        assert [group["matcher"] for group in groups] == ["^apply_patch$"]

    def test_does_not_mutate_input(self):
        claude = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"command": "${CLAUDE_PROJECT_DIR:-.}/s.py", "type": "command"}]}
                ]
            }
        }
        hook.to_codex_hooks(claude)
        assert claude["hooks"]["Stop"][0]["hooks"][0]["command"] == "${CLAUDE_PROJECT_DIR:-.}/s.py"


def test_sync_writes_hooks_only(tmp_path):
    src = tmp_path / "claude.json"
    dest = tmp_path / "hooks.json"
    src.write_text(
        json.dumps(
            {
                "env": {"X": "1"},
                "permissions": {"allow": ["Read(*)"]},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "^Edit$",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": 'python3 "${CLAUDE_PROJECT_DIR:-.}/lint.py"',
                                }
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    assert hook.sync(src, dest) == 0
    written = json.loads(dest.read_text(encoding="utf-8"))
    # Only the hooks block is carried over -- no env/permissions leak into Codex.
    assert set(written) == {"hooks"}
    command = written["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "--event PostToolUse -- python3" in command
    assert command.endswith('python3 "$(git rev-parse --show-toplevel)/lint.py"')
    # Must emit LF (repo enforces eol=lf), never CRLF, regardless of OS.
    assert b"\r\n" not in dest.read_bytes()


def test_sync_missing_src_is_noop(tmp_path):
    src = tmp_path / "missing.json"
    dest = tmp_path / "hooks.json"
    assert hook.sync(src, dest) == 0
    assert not dest.exists()
