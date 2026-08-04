#!/usr/bin/env python3
"""Regenerate .codex/hooks.json from .claude/settings.json's `hooks` block.

.claude/settings.json is the single source of truth for hook wiring. Codex reads
its hooks from .codex/hooks.json, so this generator keeps that file in lock-step
with Claude instead of hand-editing.

This is the ONLY settings artifact Codex consumes from a .claude mirror. Codex has
no .agents/settings.json concept -- inside .agents/ it reads AGENTS.md and
plugins/marketplace.json, nothing else -- so there is no second settings file to
keep in sync. Codex's own configuration lives in .codex/config.toml (approval
policy, sandbox mode, shell env) and ~/.codex/config.toml (model, reasoning
effort); Claude keys like `model: opus` or `permissions.allow` are meaningless
there and must never be mirrored into a Codex-facing file.

Codex supports most, but not all, Claude hook events and uses structured JSON
instead of non-zero exit codes for blocking. This generator therefore:

- drops explicitly classified unsupported events and matchers,
- resolves shared scripts from the git root so subdirectory sessions work, and
- wraps command hooks with the Codex compatibility adapter.

Invoked by scripts/sync-agents-context.py after the .agents/ directory mirror.

`to_codex_hooks` is pure and unit-tested
(scripts/hooks/tests/test_sync_codex_hooks.py).
"""

import json
import sys
from pathlib import Path

CLAUDE_PROJECT_DIR_PREFIX = "${CLAUDE_PROJECT_DIR:-.}/"
CODEX_ROOT_EXPR = "$(git rev-parse --show-toplevel)"
CODEX_ADAPTER = f'python3 "{CODEX_ROOT_EXPR}/scripts/hooks/codex-hook-adapter.py"'
CLAUDE_SESSION_START = ".claude/hooks/session-start.sh"
CODEX_SESSION_START = f'python3 "{CODEX_ROOT_EXPR}/scripts/hooks/codex-session-start.py"'
SUPPORTED_EVENTS = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SubagentStart",
        "SubagentStop",
        "Stop",
    }
)
UNSUPPORTED_EVENTS = frozenset({"PostToolUseFailure"})
UNSUPPORTED_MATCHERS = frozenset({("PostToolUse", "^Skill$")})


def rewrite_command(command: str) -> str:
    """Resolve Claude's project-dir placeholder through the current git root."""
    return command.replace(CLAUDE_PROJECT_DIR_PREFIX, f"{CODEX_ROOT_EXPR}/")


def wrap_command(event: str, command: str) -> str:
    """Route a shared Python/bash handler through the Codex compatibility adapter."""
    rewritten = rewrite_command(command)
    if event == "SessionStart" and CLAUDE_SESSION_START in rewritten:
        rewritten = CODEX_SESSION_START
    if not rewritten.lstrip().startswith(("python3 ", "bash ")):
        return rewritten
    return f"{CODEX_ADAPTER} --event {event} -- {rewritten}"


def to_codex_hooks(claude_settings: dict) -> dict:
    """Build Codex's `{"hooks": ...}` payload from a Claude settings dict.

    Explicitly unsupported events and dead matchers are omitted. An unclassified
    event raises so adding a Claude hook cannot silently skip Codex compatibility
    review. A missing `hooks` block yields an empty one.
    """
    hooks = claude_settings.get("hooks", {})
    unclassified_events = set(hooks) - SUPPORTED_EVENTS - UNSUPPORTED_EVENTS
    if unclassified_events:
        names = ", ".join(sorted(unclassified_events))
        raise ValueError(
            f"Unclassified Claude hook event(s): {names}. "
            "Add each event to SUPPORTED_EVENTS or UNSUPPORTED_EVENTS."
        )

    result: dict = {}
    for event, groups in hooks.items():
        if event in UNSUPPORTED_EVENTS:
            continue
        new_groups = []
        for group in groups:
            if (event, group.get("matcher", "")) in UNSUPPORTED_MATCHERS:
                continue
            new_group = dict(group)
            new_group["hooks"] = [
                {**h, "command": wrap_command(event, h["command"])}
                if isinstance(h, dict) and isinstance(h.get("command"), str)
                else h
                for h in group.get("hooks", [])
            ]
            new_groups.append(new_group)
        if new_groups:
            result[event] = new_groups
    return {"hooks": result}


def sync(src: Path, dest: Path) -> int:
    """Write `src` settings' hooks to `dest` in Codex form. No-op if src missing."""
    if not src.exists():
        return 0
    data = json.loads(src.read_text(encoding="utf-8"))
    # newline='' keeps LF on Windows (the repo enforces eol=lf via .gitattributes).
    dest.write_text(json.dumps(to_codex_hooks(data), indent=2) + "\n", encoding="utf-8", newline="")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        src, dest = Path(argv[0]), Path(argv[1])
    else:
        repo_root = Path(__file__).resolve().parent.parent
        src = repo_root / ".claude/settings.json"
        dest = repo_root / ".codex/hooks.json"
    return sync(src, dest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
