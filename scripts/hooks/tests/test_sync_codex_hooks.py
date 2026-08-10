"""Unit tests for the .codex/hooks.json generator (from .claude/settings.json)."""

import json

import pytest
from conftest import load_module

hook = load_module("scripts/sync-codex-hooks.py")
sync_devkit = load_module("scripts/sync-devkit.py")


def test_codex_runtime_and_tests_are_vendored():
    """Generated commands must not depend on files held by only one consumer."""
    required = {
        "scripts/hooks/codex-hook-adapter.py",
        "scripts/hooks/codex-session-start.py",
        "scripts/hooks/tests/test_codex_hook_adapter.py",
        "scripts/hooks/tests/test_codex_session_start.py",
    }

    assert required <= set(sync_devkit.MANIFEST)
    for relative in required:
        assert (sync_devkit.REPO_ROOT / relative).is_file(), relative


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


# --- the workstation-level pair ---------------------------------------------
# ~/.claude/settings.json wires the hooks that are NOT vendored into any repo -- the
# home-branch edit guard and the task-slug recorder. Codex had neither, so its edits
# landed on home branches with nothing catching them.


def test_a_python_command_is_wrapped_like_python3():
    """The workstation hooks are hand-written with the interpreter Windows has. A
    prefix list that only knew `python3 ` let every user-level hook through unwrapped,
    so Codex got the raw command and none of the adapter's exit-code translation."""
    wrapped = hook.wrap_command("PreToolUse", 'python "C:/devkit/scripts/guard.py"')
    assert "codex-hook-adapter.py" in wrapped
    assert "--event PreToolUse" in wrapped


def test_an_absolute_root_replaces_the_git_rev_parse_expression():
    """A Codex session opened outside any checkout makes `$(git rev-parse ...)` fail
    and takes the hook with it -- which is exactly where the workstation file is read."""
    wrapped = hook.wrap_command("PreToolUse", 'python "x/guard.py"', root="C:/ws/devkit")
    assert "C:/ws/devkit/scripts/hooks/codex-hook-adapter.py" in wrapped
    assert "git rev-parse" not in wrapped


def test_the_per_repo_form_still_resolves_through_the_git_root():
    """Repo-level files must stay repo-relative so a fresh clone works anywhere."""
    wrapped = hook.wrap_command("PreToolUse", 'python3 "x/guard.py"')
    assert "$(git rev-parse --show-toplevel)" in wrapped


def test_session_start_is_redirected_through_the_absolute_codex_entrypoint():
    wrapped = hook.wrap_command(
        "SessionStart", 'bash ".claude/hooks/session-start.sh"', root="C:/ws/devkit"
    )
    assert "C:/ws/devkit/scripts/hooks/codex-session-start.py" in wrapped


def test_stable_root_never_points_into_a_box(tmp_path, monkeypatch):
    """A box is destroyed when its PR merges. The workstation file is read for as long
    as the machine exists, so a root resolved to a box would leave every Codex hook
    invoking a deleted directory -- and running --user from a box is now the normal
    way this repo is worked on."""
    box = tmp_path / ".worktrees" / "devkit--topic-0808" / "scripts"
    box.mkdir(parents=True)
    (tmp_path / "devkit").mkdir()
    monkeypatch.setattr(hook, "__file__", str(box / "sync-codex-hooks.py"))
    assert hook.stable_root() == (tmp_path / "devkit").as_posix()


def test_stable_root_is_the_repo_itself_outside_a_box(tmp_path, monkeypatch):
    scripts = tmp_path / "devkit" / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(hook, "__file__", str(scripts / "sync-codex-hooks.py"))
    assert hook.stable_root() == (tmp_path / "devkit").as_posix()


def test_sync_creates_the_destination_directory(tmp_path):
    """~/.codex may exist while ~/.codex/hooks.json's parent chain does not on a fresh
    machine; the old code raised FileNotFoundError instead of writing."""
    src = tmp_path / "settings.json"
    src.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    dest = tmp_path / "nested" / "deeper" / "hooks.json"
    assert hook.sync(src, dest) == 0
    assert dest.is_file()


def test_the_user_pair_carries_the_guard_and_the_slug_recorder(tmp_path):
    """The regression this whole change exists for: both must reach Codex."""
    src = tmp_path / "settings.json"
    src.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": 'python "d/task_slug.py"'}]}
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "^(Edit|Write)$",
                            "hooks": [
                                {"type": "command", "command": 'python "d/worktree-guard.py"'}
                            ],
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    dest = tmp_path / "hooks.json"
    hook.sync(src, dest, root="C:/ws/devkit")
    body = dest.read_text(encoding="utf-8")
    assert "task_slug.py" in body
    assert "worktree-guard.py" in body
    assert "git rev-parse" not in body
