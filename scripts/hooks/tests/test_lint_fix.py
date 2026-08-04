"""Tests for scripts/hooks/lint-fix.py."""

import json
import subprocess
from pathlib import Path

from conftest import load_module

lint_fix = load_module("scripts/hooks/lint-fix.py")


def result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# ---- parse_hook_input ------------------------------------------------------


def test_parse_hook_input_empty_is_none():
    assert lint_fix.parse_hook_input("") is None


def test_parse_hook_input_malformed_is_none():
    assert lint_fix.parse_hook_input("{not json") is None


def test_parse_hook_input_non_dict_is_none():
    assert lint_fix.parse_hook_input("[1, 2]") is None


def test_parse_hook_input_valid_dict():
    assert lint_fix.parse_hook_input('{"tool_name": "Edit"}') == {"tool_name": "Edit"}


# ---- extract_path ----------------------------------------------------------


def test_extract_path_none_input():
    assert lint_fix.extract_path(None) is None


def test_extract_path_snake_case():
    payload = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"}}
    assert lint_fix.extract_path(payload) == "app/main.py"


def test_extract_path_camel_case():
    payload = {"toolName": "Write", "toolInput": {"filePath": "app/x.py"}}
    assert lint_fix.extract_path(payload) == "app/x.py"


def test_extract_path_rejects_other_tools():
    payload = {"tool_name": "Bash", "tool_input": {"file_path": "app/main.py"}}
    assert lint_fix.extract_path(payload) is None


def test_extract_path_missing_path_is_none():
    assert lint_fix.extract_path({"tool_name": "Edit", "tool_input": {}}) is None


def test_extract_path_non_dict_tool_input():
    assert lint_fix.extract_path({"tool_name": "Edit", "tool_input": "nope"}) is None


def test_extract_paths_from_codex_apply_patch_payload():
    patch = """*** Begin Patch
*** Update File: scripts/a.py
@@
*** Add File: scripts/b.py
+x = 1
*** Delete File: README.md
*** End Patch
"""
    payload = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
    assert lint_fix.extract_paths(payload) == [
        "scripts/a.py",
        "scripts/b.py",
        "README.md",
    ]


def test_extract_paths_from_codex_freeform_tool_input():
    patch = "*** Begin Patch\n*** Update File: scripts/a.py\n*** End Patch\n"
    payload = {"tool_name": "apply_patch", "tool_input": patch}
    assert lint_fix.extract_paths(payload) == ["scripts/a.py"]


def test_extract_paths_from_live_codex_command_payload():
    patch = "*** Begin Patch\n*** Update File: C:\\repo\\scripts\\a.py\n@@\n+x=1\n*** End Patch\n"
    payload = {"tool_name": "apply_patch", "tool_input": {"command": patch}}
    assert lint_fix.extract_paths(payload) == [r"C:\repo\scripts\a.py"]


def test_extract_paths_deduplicates_patch_targets():
    patch = "*** Update File: app/main.py\n*** Update File: app/main.py\n"
    payload = {"tool_name": "apply_patch", "tool_input": {"input": patch}}
    assert lint_fix.extract_paths(payload) == ["app/main.py"]


# ---- is_lintable -----------------------------------------------------------


def test_is_lintable_python():
    assert lint_fix.is_lintable("app/main.py")
    assert lint_fix.is_lintable("app/types.pyi")


def test_is_lintable_rejects_non_python():
    assert not lint_fix.is_lintable("README.md")
    assert not lint_fix.is_lintable("frontend/src/app.tsx")


# ---- find_ruff -------------------------------------------------------------


def test_find_ruff_prefers_venv(tmp_path):
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    ruff = venv_bin / "ruff"
    ruff.write_text("#!/bin/sh\n")
    assert lint_fix.find_ruff(tmp_path) == str(ruff)


def test_find_ruff_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.setattr(lint_fix.shutil, "which", lambda name: "/usr/bin/ruff")
    assert lint_fix.find_ruff(tmp_path) == "/usr/bin/ruff"


# ---- main ------------------------------------------------------------------


def _payload(path: str) -> str:
    # json.dumps, not an f-string: a Windows tmp_path (C:\Users\...\Temp\...) embedded
    # raw makes \U/\A/\T invalid JSON escapes, so parse_hook_input returns None and
    # main() returns 0 before reaching the branch under test.
    return json.dumps({"tool_name": "Edit", "tool_input": {"file_path": path}})


def _patch_payload(*paths: str) -> str:
    patch = "\n".join(f"*** Update File: {path}" for path in paths)
    return json.dumps({"tool_name": "apply_patch", "tool_input": {"input": patch}})


def test_payload_survives_windows_paths():
    # Regression: the helper used to interpolate the path into JSON with an f-string,
    # so a Windows tmp_path silently failed to parse and every main() test below that
    # asserts `== 0` passed vacuously (main() returns 0 on an unparseable payload).
    win = r"C:\Users\Admin\AppData\Local\Temp\pytest-1\test_x0\x.py"
    assert lint_fix.extract_path(lint_fix.parse_hook_input(_payload(win))) == win


def test_main_skips_non_python(monkeypatch):
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload("README.md"))
    called = []
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: called.append("ruff") or "ruff")
    assert lint_fix.main() == 0
    assert called == []  # returned before resolving ruff


def test_main_noop_when_ruff_missing(monkeypatch, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: None)
    assert lint_fix.main() == 0


def test_main_clean_file_returns_zero(monkeypatch, tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    monkeypatch.setattr(lint_fix, "_run", lambda ruff, *args: result(returncode=0))
    assert lint_fix.main() == 0


def test_main_relays_remaining_errors(monkeypatch, tmp_path, capsys):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n")
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(f)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")

    calls = []

    def fake_run(ruff, *args):
        calls.append(args)
        # `format` and `check --fix` succeed silently; the final reporting
        # `check` (no --fix) surfaces the unfixable finding.
        if args[0] == "check" and "--fix" not in args:
            return result(returncode=1, stdout="x.py:1:1: F821 undefined name\n")
        return result(returncode=0)

    monkeypatch.setattr(lint_fix, "_run", fake_run)

    assert lint_fix.main() == 2
    err = capsys.readouterr().err
    assert "F821" in err
    # Fixers ran before the reporting check.
    assert ("format", str(f)) in calls
    assert ("check", "--fix", str(f)) in calls


def test_main_missing_file_returns_zero(monkeypatch, tmp_path):
    missing = tmp_path / "gone.py"
    monkeypatch.setattr(lint_fix, "_read_stdin", lambda: _payload(str(missing)))
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    assert lint_fix.main() == 0


def test_main_lints_every_python_file_in_codex_patch(monkeypatch, tmp_path):
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("x = 1\n")
    second.write_text("y = 2\n")
    monkeypatch.setattr(
        lint_fix,
        "_read_stdin",
        lambda: _patch_payload(str(first), str(second), "README.md"),
    )
    monkeypatch.setattr(lint_fix, "find_ruff", lambda root: "ruff")
    calls = []
    monkeypatch.setattr(
        lint_fix,
        "_run",
        lambda ruff, *args: calls.append(args) or result(returncode=0),
    )

    assert lint_fix.main() == 0
    assert ("format", str(first)) in calls
    assert ("format", str(second)) in calls
    assert not any("README.md" in call for args in calls for call in args)


# ---- ruff_arg: repo-relative POSIX so per-file-ignores globs match ----------


def test_ruff_arg_repo_relative_posix_inside_project():
    target = lint_fix.REPO_ROOT / "scripts" / "hooks" / "stop.py"
    assert lint_fix.ruff_arg(target, lint_fix.REPO_ROOT) == "scripts/hooks/stop.py"


def test_ruff_arg_relativises_despite_lowercase_drive():
    # Regression: Claude Code's payload sends a lowercase-drive absolute path
    # (`c:\...`) while the hook runs with an uppercase-drive cwd. Passing that
    # straight to ruff defeated per-file-ignores; ruff_arg must still relativise
    # it (resolve() canonicalises the drive case) to the same repo-relative path.
    root = lint_fix.REPO_ROOT
    lower_drive_root = Path(str(root)[:1].lower() + str(root)[1:])
    target = lower_drive_root / "scripts" / "hooks" / "stop.py"
    assert lint_fix.ruff_arg(target, root) == "scripts/hooks/stop.py"


def test_ruff_arg_absolute_when_outside_project(tmp_path):
    outside = tmp_path / "x.py"
    assert lint_fix.ruff_arg(outside, lint_fix.REPO_ROOT) == str(outside)
