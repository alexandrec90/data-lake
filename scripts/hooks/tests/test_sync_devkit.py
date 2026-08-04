"""Unit tests for scripts/sync-devkit.py (harness vendoring + drift check)."""

from pathlib import Path

from conftest import load_module

sh = load_module("scripts/sync-devkit.py")


def test_resolve_src_prefers_arg_then_env():
    assert sh.resolve_src("/a/b", {sh.SRC_ENV: "/c"}) == Path("/a/b").expanduser().resolve()
    assert sh.resolve_src(None, {sh.SRC_ENV: "/c"}) == Path("/c").expanduser().resolve()
    assert sh.resolve_src(None, {}) is None


def _seed(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_classify_partitions_ok_drift_missing(tmp_path):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    manifest = ("scripts/a.py", "scripts/b.py", "scripts/c.py")
    _seed(src, "scripts/a.py", "same")
    _seed(repo, "scripts/a.py", "same")  # ok
    _seed(src, "scripts/b.py", "upstream")
    _seed(repo, "scripts/b.py", "local-edit")  # drift
    _seed(repo, "scripts/c.py", "only-here")  # missing in src

    drifted, missing, ok = sh.classify(src, repo, manifest)
    assert ok == ["scripts/a.py"]
    assert drifted == ["scripts/b.py"]
    assert missing == ["scripts/c.py"]


def test_check_noop_when_src_unset(capsys, monkeypatch):
    monkeypatch.delenv(sh.SRC_ENV, raising=False)
    assert sh.main(["--check"]) == 0
    assert "skipping" in capsys.readouterr().out


def test_check_passes_when_in_sync(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--check", "--src", str(src)]) == 0


def test_check_fails_on_drift(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, "scripts/x.py", "local")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--check", "--src", str(src)]) == 1


def test_pull_copies_shared_into_project(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert (repo / "scripts/x.py").read_text() == "upstream"


def test_pull_removes_only_reviewed_retired_files(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/SKILL.md", "obsolete")
    _seed(repo, ".claude/skills/old/state.json", "project-owned")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/SKILL.md",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert not (repo / ".claude/skills/old/SKILL.md").exists()
    assert (repo / ".claude/skills/old/state.json").read_text() == "project-owned"


def test_pull_receipt_removes_a_no_longer_managed_unchanged_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src)]) == 0

    _seed(src, "scripts/new.py", "new")
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/new.py",))
    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert not (repo / "scripts/old.py").exists()
    assert (repo / "scripts/new.py").read_text() == "new"


def test_pull_receipt_preserves_a_locally_edited_retired_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src)]) == 0
    _seed(repo, "scripts/old.py", "local edit")

    monkeypatch.setattr(sh, "MANIFEST", ())
    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert (repo / "scripts/old.py").read_text() == "local edit"


def test_check_fails_while_a_retired_file_is_present(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "same")
    _seed(repo, "scripts/x.py", "same")
    _seed(repo, ".claude/skills/old/SKILL.md", "obsolete")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/SKILL.md",))

    assert sh.main(["--check", "--src", str(src)]) == 1


def test_push_copies_project_into_shared(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(repo, "scripts/x.py", "authored-here")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    assert sh.main(["--push", "--src", str(src)]) == 0
    assert (src / "scripts/x.py").read_text() == "authored-here"


def test_list_prints_manifest(capsys):
    assert sh.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "scripts/hooks/harness_config.py" in out


def test_manifest_files_exist_in_repo():
    # The vendored manifest must reference real files in this repo.
    for rel in sh.MANIFEST:
        assert (sh.REPO_ROOT / rel).exists(), f"manifest lists missing file: {rel}"


def test_version_file_not_in_manifest():
    # DEVKIT_VERSION is a per-project artifact, never synced/drift-checked.
    assert sh.VERSION_FILE not in sh.MANIFEST
    assert sh.RECEIPT_FILE not in sh.MANIFEST


# ---- version stamping ------------------------------------------------------


def test_read_version_roundtrip(tmp_path):
    assert sh.read_version(tmp_path) is None
    (tmp_path / sh.VERSION_FILE).write_text("abc1234\n")
    assert sh.read_version(tmp_path) == "abc1234"


def test_git_head_parses_sha(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        sh.subprocess, "run", lambda *a, **k: _sp.CompletedProcess([], 0, "deadbee\n", "")
    )
    assert sh.git_head(Path(".")) == "deadbee"


def test_git_head_none_on_failure(monkeypatch):
    import subprocess as _sp

    monkeypatch.setattr(
        sh.subprocess, "run", lambda *a, **k: _sp.CompletedProcess([], 128, "", "not a git repo")
    )
    assert sh.git_head(Path(".")) is None


def test_pull_stamps_harness_version(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert (repo / sh.VERSION_FILE).read_bytes() == b"abc1234\n"
