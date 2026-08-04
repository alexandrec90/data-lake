"""Tests for scripts/sync-agents-context.py pure tree helpers."""

from conftest import load_module

mod = load_module("scripts/sync-agents-context.py")


def test_iter_files_skips_prune_dirs(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.txt").write_text("b", encoding="utf-8")
    names = {p.name for p in mod.iter_files(tmp_path)}
    assert names == {"a.txt"}


def test_iter_files_skips_nested_prune_dirs(tmp_path):
    # Prune dirs anywhere in the tree (e.g. frontend/node_modules) are skipped.
    nested = tmp_path / "frontend" / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    (nested / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "frontend" / "a.txt").write_text("a", encoding="utf-8")
    names = {p.name for p in mod.iter_files(tmp_path)}
    assert names == {"a.txt"}


def test_sync_claude_md_copies_and_purges(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("hello", encoding="utf-8")
    # An orphan AGENTS.md with no sibling CLAUDE.md should be purged.
    orphan = tmp_path / "old"
    orphan.mkdir()
    (orphan / "AGENTS.md").write_text("stale", encoding="utf-8")

    copied, deleted = mod.sync_claude_md(tmp_path)
    assert copied == 1
    assert deleted == 1
    assert (sub / "AGENTS.md").read_text(encoding="utf-8") == "hello"
    assert not (orphan / "AGENTS.md").exists()


def test_mirror_tree_copies_changed_and_removes_orphans(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    (src / "nested").mkdir(parents=True)
    (src / "keep.txt").write_text("new", encoding="utf-8")
    (src / "nested" / "deep.txt").write_text("deep", encoding="utf-8")
    dest.mkdir()
    (dest / "orphan.txt").write_text("remove me", encoding="utf-8")
    (dest / "keep.txt").write_text("old", encoding="utf-8")

    mod.mirror_tree(src, dest)

    assert (dest / "keep.txt").read_text(encoding="utf-8") == "new"
    assert (dest / "nested" / "deep.txt").read_text(encoding="utf-8") == "deep"
    assert not (dest / "orphan.txt").exists()


def test_mirror_tree_excludes_agent_settings_files(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    for name in ("settings.json", "settings.local.json"):
        (src / name).write_text("SRC", encoding="utf-8")
        (dest / name).write_text("DEST", encoding="utf-8")

    mod.mirror_tree(src, dest, exclude_names=mod.AGENT_MIRROR_EXCLUDE_NAMES)

    # Excluded files are neither overwritten nor deleted.
    for name in ("settings.json", "settings.local.json"):
        assert (dest / name).read_text(encoding="utf-8") == "DEST"
