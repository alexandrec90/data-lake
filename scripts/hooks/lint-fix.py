#!/usr/bin/env python3
"""PostToolUse hook: auto-fix just-edited Python files and relay what remains.

Runs after Edit/Write/MultiEdit/apply_patch on `.py` files. It applies the cheap,
deterministic fixers in place (`ruff format`, then `ruff check --fix`), so
formatting drift never survives to a commit — the CI lint job no longer gates on
it. Any lint finding ruff *cannot* auto-fix (undefined name, real bug pattern) is
printed to stderr with exit code 2, which Claude Code feeds straight back into the
coding agent's turn so it fixes the issue before finishing — no CI round-trip.

Deliberately ruff-only and edit-scoped to stay fast: the slower,
cross-file checks (mypy / vulture / frontend) belong in the Stop backstop and CI,
not on every keystroke. Best-effort throughout: a missing ruff or an unreadable
payload exits 0 so a tooling gap never blocks the agent.

Pure helpers (`parse_hook_input`, `extract_paths`, `is_lintable`, `find_ruff`) are
unit-tested in `scripts/hooks/tests/test_lint_fix.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ALLOWED_TOOLS = {"Edit", "Write", "MultiEdit", "apply_patch", "create_file"}
REPO_ROOT = (Path(__file__).parent / "../..").resolve()
PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$",
    re.MULTILINE,
)


def parse_hook_input(raw: str) -> dict | None:
    """Parse raw stdin into a dict, or None when absent/malformed."""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def extract_paths(hook_input: dict | None) -> list[str]:
    """Return edited paths for an allowed tool, including freeform patches.

    Tolerates both snake_case and camelCase payload keys (tool_name/toolName,
    tool_input/toolInput, file_path/filePath). Codex sends apply_patch content
    under ``tool_input.input`` instead of a file_path.
    """
    if not hook_input:
        return []
    tool = hook_input.get("tool_name") or hook_input.get("toolName") or ""
    if tool and tool not in ALLOWED_TOOLS:
        return []
    tool_input = hook_input.get("tool_input") or hook_input.get("toolInput") or {}

    found: list[str] = []
    patch: object
    if isinstance(tool_input, str):
        patch = tool_input
    elif isinstance(tool_input, dict):
        path = tool_input.get("file_path") or tool_input.get("filePath")
        if isinstance(path, str) and path:
            found.append(path)
        patch = tool_input.get("input") or tool_input.get("patch") or tool_input.get("command")
    else:
        return []
    if isinstance(patch, str):
        found.extend(PATCH_PATH_RE.findall(patch))

    return list(dict.fromkeys(found))


def extract_path(hook_input: dict | None) -> str | None:
    """Backward-compatible first edited path, or None."""
    paths = extract_paths(hook_input)
    return paths[0] if paths else None


def is_lintable(path: str) -> bool:
    """True for Python source files ruff should format and check."""
    return path.endswith((".py", ".pyi"))


def ruff_arg(target: Path, repo_root: Path) -> str:
    """Path form to hand ruff: repo-relative POSIX when `target` is inside the
    project, else the absolute path.

    ruff matches `per-file-ignores` globs (e.g. `scripts/**/*.py`) against the file
    path relativised to the config-file directory. Handing ruff an absolute path
    makes that relativisation depend on exact drive-case / separator normalisation
    between the arg and ruff's discovered project root; when they differ, ruff
    treats the file as outside the project and the ignores silently do not apply,
    so ignored rules (T201/S603/S607, and S101 in tests) fire as false positives
    that block the edit. A repo-relative POSIX path needs no relativisation and
    matches the globs directly. `target`/`repo_root` are resolved first so a
    lowercase-drive payload path still relativises against the resolved repo root.
    """
    try:
        return target.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(target)


def find_ruff(repo_root: Path) -> str | None:
    """Resolve the ruff executable, preferring the project venv over PATH."""
    for sub in ("Scripts", "bin"):
        for name in ("ruff.exe", "ruff"):
            cand = repo_root / ".venv" / sub / name
            if cand.exists():
                return str(cand)
    return shutil.which("ruff")


def _run(ruff: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ruff, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


def main() -> int:
    hook_input = parse_hook_input(_read_stdin())
    paths = [path for path in extract_paths(hook_input) if is_lintable(path)]
    if not paths:
        return 0

    ruff = find_ruff(REPO_ROOT)
    if not ruff:
        return 0

    failures: list[str] = []
    for path in paths:
        target = Path(path)
        if not target.is_absolute():
            target = REPO_ROOT / path
        if not target.exists():
            continue

        # Hand ruff the repo-relative POSIX path so `per-file-ignores` globs match
        # deterministically. Claude Code's hook payload sends `file_path` as a
        # lowercase-drive absolute path (`c:\...`) while this process runs with an
        # uppercase-drive cwd (`C:\...`); ruff relativises an absolute arg against
        # its resolved project root to match the globs, and that drive-case mismatch
        # makes it treat the file as outside the project -- silently dropping the
        # ignores so T201/S603/S607 (and S101 in tests) fire as false positives.
        file_arg = ruff_arg(target, REPO_ROOT)
        # Deterministic auto-fixers first, silently: formatting and import sorting
        # should never reach the agent as "errors" — they just get applied.
        _run(ruff, "format", file_arg)
        _run(ruff, "check", "--fix", file_arg)

        # Whatever remains is a genuine finding ruff can't fix on its own.
        remaining = _run(ruff, "check", file_arg, "--output-format=concise")
        if remaining.returncode != 0:
            detail = (remaining.stdout + remaining.stderr).strip()
            failures.append(f"ruff found issues in {path} that need a manual fix:\n{detail}")

    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 2
    return 0


def _read_stdin() -> str:
    """Best-effort read of the hook payload; '' when stdin is a tty or unreadable."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


if __name__ == "__main__":
    sys.exit(main())
