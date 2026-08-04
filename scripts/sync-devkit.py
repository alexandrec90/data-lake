#!/usr/bin/env python3
"""Vendor the shared agent-harness scripts into this project, and drift-check them.

The hook/harness scripts are meant to be identical across projects (see
`.devkit.toml` for the per-project seam). Rather than fork them per repo,
one **shared harness repo is the source of truth** and each project commits a
**vendored copy** -- so cloning a single project still gets everything, with no
submodule. This script keeps that copy honest:

  - `--check` (default): fail (exit 1) if any vendored file drifts from the shared
    repo. Wired into the PR gate. **No-ops clean (exit 0) when the shared repo is
    not configured**, so CI is safe before a project adopts the shared repo.
  - `--pull`: copy the shared repo's version into this project (adopt upstream).
    Also remove exact paths retired by the shared repo; project-owned skill state
    is never included in that retirement list.
  - `--push`: copy this project's version into the shared repo (author a change /
    seed a fresh shared repo from the project that currently owns the code).
  - `--list`: print the manifest and resolved source, then exit.

The shared-repo path resolves from `--src` or `$DEVKIT_DIR`. `.devkit.toml`
itself is **never** synced -- it is the per-project config the shared code reads.

`MANIFEST` is the reviewed, portable subset (config loader + the scripts audited so
far); extend it as more scripts are decoupled. Pure helpers (`resolve_src`,
`classify`) are unit-tested in `scripts/hooks/tests/test_sync_devkit.py`.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = (Path(__file__).parent / "..").resolve()
SRC_ENV = "DEVKIT_DIR"
# Records which shared-repo commit this project's vendored copy corresponds to.
# Committed, per-project (like .devkit.toml), so NOT in MANIFEST.
VERSION_FILE = "DEVKIT_VERSION"
# Receipt of the exact files the last pull managed, with content hashes. Unlike the
# version stamp this is operational: it lets a later manifest retire paths safely
# without growing a permanent tombstone list. It is per-project and never vendored.
RECEIPT_FILE = "DEVKIT_FILES.json"

# Repo-relative paths of the shared harness files (source of truth = shared repo).
# Every entry ships with its test; keep both in the manifest so a vendored copy is
# verifiable in isolation. NB: `.devkit.toml` is intentionally absent -- it
# is per-project config, not shared code.
#
# The `.claude -> .agents/.codex` mirror scripts are in, near the bottom -- this note
# used to say they were pending. `sync-agents-context.py` in particular now backs a
# shared workspace task (`devkit_project.ACTIONS["sync-agents"]`), so every consuming
# project is expected to have it at that exact path and the drift check is what keeps
# that true.
MANIFEST: tuple[str, ...] = (
    # Test plumbing: the load_module() loader every vendored test imports.
    "scripts/hooks/tests/conftest.py",
    # Repo-shape contract: the vendored scripts' unvendored dependencies exist, and
    # the manifest that selects them is spelled right. No script of its own -- it
    # asserts against whatever the consuming repo already has.
    "scripts/hooks/tests/test_repo_contract.py",
    # Config loader (the per-project seam) + the Stop dispatcher it drives.
    "scripts/hooks/harness_config.py",
    "scripts/hooks/tests/test_harness_config.py",
    "scripts/hooks/stop.py",
    "scripts/hooks/tests/test_stop.py",
    # Auto-fix-on-edit PostToolUse hook (repo-relative ruff path fix).
    "scripts/hooks/lint-fix.py",
    "scripts/hooks/tests/test_lint_fix.py",
    # Bash output cap: the PreToolUse gate and the wrapper it demands. They ship
    # together because the gate's allow-list matches the wrapper's path -- vendoring
    # one without the other yields a hook that blocks every Bash call and names a
    # remedy the repo does not have. Cap size is `[bash]` in the manifest.
    "scripts/hooks/enforce-capped-bash.py",
    "scripts/hooks/tests/test_enforce_capped_bash.py",
    "scripts/hooks/invoke-capped.py",
    "scripts/hooks/tests/test_invoke_capped.py",
    # Branch lifecycle: default-branch auto-detected (detect_default_branch), so
    # these vendor unchanged. session-start.sh is the SessionStart entrypoint.
    "scripts/task_branch.py",
    "scripts/hooks/tests/test_task_branch.py",
    "scripts/ship.py",
    "scripts/hooks/tests/test_ship.py",
    "scripts/hooks/session-sync.py",
    "scripts/hooks/tests/test_session_sync.py",
    "scripts/hooks/branch-per-task.py",
    ".claude/hooks/session-start.sh",
    "scripts/hooks/tests/test_session_start.py",
    # The vendoring tool itself, so a project can drift-check / pull / push.
    "scripts/sync-devkit.py",
    "scripts/hooks/tests/test_sync_devkit.py",
    # --- The shared instruction tier -----------------------------------------
    # Same argument as the scripts, applied to the prose that steers the agent. These
    # paragraphs used to live inline in each repo's CLAUDE.md, get copied forward by
    # hand, and drift: devkit's own template had already lost a clause of the testing
    # mandate that carameli still had, and nothing could detect it. A project's
    # CLAUDE.md now points at these instead of restating them.
    #
    # Only genuinely portable files belong here. A rule or skill that names one
    # project's paths, services, or default branch is that project's own -- vendoring
    # it repeats the mistake that made every generated project fail 12 tests on its
    # first CI run.
    ".claude/rules/engineering.md",
    ".claude/rules/authoring.md",
    # The one portable task workflow. Its script detects the remote default branch
    # and owns the mechanical checks; the skill supplies the semantic commit/PR text.
    ".claude/skills/ship/SKILL.md",
    # The .claude -> AGENTS.md/.agents mirror, so a project's Codex-facing tree is
    # generated rather than hand-maintained. `sync-codex-hooks.py` only fires when the
    # project has a `.codex/` directory, so this is inert until a repo opts in.
    # NB: carameli's test_codex_hooks_contract.py is deliberately NOT vendored -- it
    # pins that repo's exact hook topology, which is the coupling this tier exists to
    # avoid. It belongs in a project's own non-vendored suite.
    "scripts/sync-agents-context.py",
    "scripts/hooks/tests/test_sync_agents_context.py",
    "scripts/sync-codex-hooks.py",
    "scripts/hooks/tests/test_sync_codex_hooks.py",
)

# Exact formerly-vendored files removed by `--pull`. Mirrors are included because
# `.agents/` is generated from `.claude/`; leaving an old mirrored SKILL.md would keep
# the deleted command alive for Codex. Project-owned files such as state.json and
# known-fixes.md are deliberately absent: retiring shared prose must not erase local data.
_RETIRED_CLAUDE_PATHS: tuple[str, ...] = (
    ".claude/skills/audit-claude-md/SKILL.md",
    ".claude/skills/audit-dockerignore/SKILL.md",
    ".claude/skills/audit-gitignore/SKILL.md",
    ".claude/skills/fix-pre-commit/SKILL.md",
    ".claude/skills/plan-handoff/SKILL.md",
    ".claude/skills/refactor/SKILL.md",
    ".claude/skills/retro/SKILL.md",
    ".claude/skills/retro/extract.py",
    ".claude/skills/task/SKILL.md",
    ".claude/skills/test-skill/SKILL.md",
    ".claude/skills/test-skill/write-artifacts.py",
    ".claude/skills/state-tools/state-engine.py",
    ".claude/skills/state-tools/README.md",
)
_RETIRED_HARNESS_PATHS: tuple[str, ...] = (
    "scripts/hooks/normalize-known-fixes.py",
    "scripts/hooks/tests/test_normalize_known_fixes.py",
    "scripts/hooks/finalize-state.py",
    "scripts/hooks/tests/test_finalize_state.py",
    "scripts/hooks/tests/test_state_engine.py",
    "scripts/hooks/archive-session.py",
    "scripts/hooks/tests/test_archive_session_outcomes.py",
    "scripts/hooks/tests/test_archive_session_stdin.py",
    "scripts/hooks/tests/test_archive_session_tokens.py",
)
RETIRED_PATHS: tuple[str, ...] = (
    _RETIRED_HARNESS_PATHS
    + _RETIRED_CLAUDE_PATHS
    + tuple(path.replace(".claude/", ".agents/", 1) for path in _RETIRED_CLAUDE_PATHS)
)


def resolve_src(arg: str | None, env: Mapping[str, str]) -> Path | None:
    """The shared-repo root from `--src` or `$DEVKIT_DIR`, or None when unset."""
    raw = arg or env.get(SRC_ENV)
    return Path(raw).expanduser().resolve() if raw else None


def read_version(root: Path) -> str | None:
    """The stamped harness commit this project vendored, or None if never pulled."""
    try:
        return (root / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


def git_head(path: Path) -> str | None:
    """Short HEAD SHA of the git repo at `path`, or None (not a repo / no git)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _sha256(path: Path) -> str | None:
    data = _read(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_receipt(root: Path) -> dict[str, str]:
    """Return {managed path: sha256} from the last pull; malformed receipts are empty."""
    try:
        raw = json.loads((root / RECEIPT_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        path: digest
        for path, digest in files.items()
        if isinstance(path, str) and isinstance(digest, str)
    }


def write_receipt(root: Path, manifest: tuple[str, ...]) -> None:
    files = {rel: digest for rel in manifest if (digest := _sha256(root / rel)) is not None}
    payload = {"version": 1, "files": files}
    (root / RECEIPT_FILE).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def classify(
    src: Path, repo_root: Path, manifest: tuple[str, ...]
) -> tuple[list[str], list[str], list[str]]:
    """Partition the manifest into (drifted, missing_in_src, ok) by byte comparison.

    `missing_in_src` are files absent from the shared repo (it does not have them
    yet -- e.g. before a first `--push`); they are reported, never silently OK.
    """
    drifted: list[str] = []
    missing: list[str] = []
    ok: list[str] = []
    for rel in manifest:
        src_bytes = _read(src / rel)
        if src_bytes is None:
            missing.append(rel)
            continue
        if _read(repo_root / rel) == src_bytes:
            ok.append(rel)
        else:
            drifted.append(rel)
    return drifted, missing, ok


def _copy(rel: str, from_root: Path, to_root: Path) -> bool:
    """Copy one manifest file from_root -> to_root. False when the source is absent."""
    source = from_root / rel
    if not source.exists():
        return False
    dest = to_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def retired_present(root: Path) -> list[str]:
    return [rel for rel in RETIRED_PATHS if (root / rel).is_file()]


def remove_retired(root: Path) -> list[str]:
    """Delete only reviewed retired files, never project-owned sibling state."""
    removed = retired_present(root)
    for rel in removed:
        path = root / rel
        path.unlink()
        parent = path.parent
        with contextlib.suppress(OSError):
            parent.rmdir()  # only succeeds when the skill directory is now empty
    return removed


def receipt_retired_present(root: Path, manifest: tuple[str, ...]) -> list[str]:
    current = set(manifest)
    return sorted(
        rel for rel in read_receipt(root) if rel not in current and (root / rel).is_file()
    )


def remove_receipt_retired(root: Path, manifest: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Remove no-longer-managed files only when unchanged since the previous pull."""
    receipt = read_receipt(root)
    current = set(manifest)
    removed: list[str] = []
    preserved: list[str] = []
    for rel, expected in sorted(receipt.items()):
        path = root / rel
        if rel in current or not path.is_file():
            continue
        if _sha256(path) != expected:
            preserved.append(rel)
            continue
        path.unlink()
        removed.append(rel)
        with contextlib.suppress(OSError):
            path.parent.rmdir()
    return removed, preserved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail on drift (default)")
    mode.add_argument("--pull", action="store_true", help="copy shared repo -> this project")
    mode.add_argument("--push", action="store_true", help="copy this project -> shared repo")
    mode.add_argument("--list", action="store_true", help="print manifest + source, then exit")
    parser.add_argument("--src", help=f"shared harness repo root (else ${SRC_ENV})")
    args = parser.parse_args(argv)

    src = resolve_src(args.src, os.environ)

    if args.list:
        print(f"source: {src or '(unset)'}")
        print(f"vendored version: {read_version(REPO_ROOT) or '(never pulled)'}")
        for rel in MANIFEST:
            print(f"  {rel}")
        if RETIRED_PATHS:
            print("retired on pull:")
            for rel in RETIRED_PATHS:
                print(f"  {rel}")
        return 0

    if src is None:
        # Unconfigured: every mode is a clean no-op so the PR gate passes pre-adoption.
        print(f"sync-harness: ${SRC_ENV} unset and no --src; nothing to do (skipping).")
        return 0

    if args.pull or args.push:
        from_root, to_root = (src, REPO_ROOT) if args.pull else (REPO_ROOT, src)
        managed_removed, preserved = (
            remove_receipt_retired(REPO_ROOT, MANIFEST) if args.pull else ([], [])
        )
        copied = [rel for rel in MANIFEST if _copy(rel, from_root, to_root)]
        skipped = [rel for rel in MANIFEST if rel not in copied]
        removed = (remove_retired(REPO_ROOT) + managed_removed) if args.pull else []
        verb = "pulled" if args.pull else "pushed"
        print(
            f"sync-harness: {verb} {len(copied)} file(s); "
            f"removed {len(removed)} retired; skipped {len(skipped)} absent."
        )
        for rel in skipped:
            print(f"  (absent) {rel}")
        for rel in preserved:
            print(f"  (preserved local edit) {rel}")
        if args.pull:
            # Stamp which shared-repo commit this vendored copy now corresponds to.
            (REPO_ROOT / VERSION_FILE).write_text(
                f"{git_head(src) or 'unknown'}\n", encoding="utf-8", newline="\n"
            )
            write_receipt(REPO_ROOT, MANIFEST)
        return 0

    # Default: --check
    vendored, available = read_version(REPO_ROOT), git_head(src)
    if available and vendored and vendored != available:
        # Informational only -- drift is decided by content below, not version.
        print(f"sync-harness: vendored {vendored}, shared repo at {available} (newer available).")
    drifted, missing, _ = classify(src, REPO_ROOT, MANIFEST)
    retired = retired_present(REPO_ROOT)
    receipt_retired = receipt_retired_present(REPO_ROOT, MANIFEST)
    if not drifted and not missing and not retired and not receipt_retired:
        print(f"sync-harness: all {len(MANIFEST)} vendored files in sync with {src}.")
        return 0
    for rel in drifted:
        print(f"DRIFT   {rel}", file=sys.stderr)
    for rel in missing:
        print(f"MISSING {rel} (not in shared repo)", file=sys.stderr)
    for rel in retired:
        print(f"RETIRED {rel} (run --pull to remove)", file=sys.stderr)
    for rel in receipt_retired:
        print(f"RETIRED {rel} (recorded by {RECEIPT_FILE})", file=sys.stderr)
    print(
        "sync-harness: vendored harness drifted from the shared repo. "
        "Run `python scripts/sync-devkit.py --pull` to adopt upstream, "
        "or `--push` if this project authored the change.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
