#!/usr/bin/env python3
"""Syncs CLAUDE.md -> AGENTS.md (all subdirs) and .claude/ -> .agents/.

CLAUDE.md / .claude/ are the source of truth; run this after editing them.

  1. Copy every CLAUDE.md to AGENTS.md in the same directory.
  2. Purge orphaned AGENTS.md (no sibling CLAUDE.md) -- AGENTS.md is derived.
  3. Mirror .claude/ -> .agents/ (copy new/changed, remove orphans), excluding
     settings.json and settings.local.json. Codex reads AGENTS.md and
     .agents/plugins/marketplace.json out of .agents/ -- it has no notion of an
     .agents/settings.json, and machine-local config must not be mirrored.
  4. Regenerate .codex/hooks.json from the .claude/settings.json hooks block,
     path-transformed for Codex (via sync-codex-hooks.py).

Codex's own settings live in .codex/config.toml (approval/sandbox/env) and
~/.codex/config.toml (model, reasoning effort) -- never in .agents/.

The pure tree helpers are unit-tested in
`scripts/hooks/tests/test_sync_agents_context.py`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PRUNE_DIRS = {".git", "node_modules", ".venv", "__pycache__"}
AGENT_MIRROR_EXCLUDE_NAMES = frozenset({"settings.json", "settings.local.json"})


def iter_files(root: Path):
    """Yield files under root, pruning PRUNE_DIRS so their subtrees are never walked.

    rglob would stat every entry inside node_modules/.venv (~60k files) before
    filtering; pruning dirnames in-place keeps this a sub-second walk.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def sync_claude_md(root: Path) -> tuple[int, int]:
    """Copy CLAUDE.md -> AGENTS.md everywhere; purge orphan AGENTS.md. (copied, deleted)."""
    copied = deleted = 0
    claude_files: list[Path] = []
    agents_files: list[Path] = []
    for path in iter_files(root):
        if path.name == "CLAUDE.md":
            claude_files.append(path)
        elif path.name == "AGENTS.md":
            agents_files.append(path)
    for claude in claude_files:
        shutil.copyfile(claude, claude.with_name("AGENTS.md"))
        copied += 1
    for agents in agents_files:
        if not agents.with_name("CLAUDE.md").exists():
            agents.unlink()
            deleted += 1
    return copied, deleted


def mirror_tree(src: Path, dest: Path, exclude_names=frozenset()) -> None:
    """Mirror src -> dest like `robocopy /MIR`: copy new/changed, delete orphans.

    Files named in `exclude_names` are neither copied nor deleted (caller owns them).
    """
    dest.mkdir(parents=True, exist_ok=True)
    src_rel = {
        p.relative_to(src) for p in src.rglob("*") if p.is_file() and p.name not in exclude_names
    }

    # Copy new/changed files.
    for rel in src_rel:
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / rel, target)

    # Delete orphan files (present in dest, absent in src).
    for path in list(dest.rglob("*")):
        if (
            path.is_file()
            and path.name not in exclude_names
            and path.relative_to(dest) not in src_rel
        ):
            path.unlink()

    # Remove now-empty directories (deepest first).
    for path in sorted(
        (p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    ):
        if not any(path.iterdir()):
            path.rmdir()


def main() -> int:
    copied, deleted = sync_claude_md(REPO_ROOT)
    print(f"  CLAUDE.md -> AGENTS.md: {copied} copied, {deleted} orphan(s) deleted")

    claude_dir = REPO_ROOT / ".claude"
    agents_dir = REPO_ROOT / ".agents"
    if not claude_dir.exists():
        print("  skipped  .claude/ (not found)")
        return 0

    mirror_tree(claude_dir, agents_dir, exclude_names=AGENT_MIRROR_EXCLUDE_NAMES)
    print("  mirrored .claude/ -> .agents/ (settings files excluded)")

    claude_settings = claude_dir / "settings.json"
    if claude_settings.exists():
        codex_hooks = REPO_ROOT / ".codex" / "hooks.json"
        if codex_hooks.parent.exists():
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "scripts" / "sync-codex-hooks.py"),
                    str(claude_settings),
                    str(codex_hooks),
                ],
                cwd=REPO_ROOT,
            )
            if result.returncode != 0:
                print("sync-codex-hooks.py failed", file=sys.stderr)
                return 1
            print("  regenerated .codex/hooks.json (from .claude/settings.json hooks)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
