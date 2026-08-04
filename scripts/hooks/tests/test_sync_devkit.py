"""Unit tests for scripts/sync-devkit.py (harness vendoring + drift check)."""

import re
import subprocess
from pathlib import Path

import pytest
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


# --- the pin: the third thing an upgrade has to move ------------------------
# Files, DEVKIT_VERSION and the .pre-commit-config.yaml `rev:` describe one
# upstream revision. When only two of them move, the commit-time gate compares
# against the revision the pin names and reports every file added upstream since
# as drift -- a diagnosis that points at the files and never at the pin.

CONFIG = f"""\
repos:
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-yaml
  # A tag, never a branch -- one bad upstream commit must not redden this repo.
  - repo: {sh.DEVKIT_REPO}
    rev: v0.5.2 # keep in step with DEVKIT_VERSION
    hooks:
      - id: devkit-drift
"""


def test_the_pin_moves_to_the_pulled_tag():
    updated, previous = sh.bump_pin(CONFIG, "v0.5.3")
    assert previous == "v0.5.2"
    assert "rev: v0.5.3" in updated


def test_bumping_the_pin_leaves_other_repos_alone():
    """Only devkit's pin moves. Retargeting a third-party hook to a devkit tag
    would break the hook and be invisible until the next commit."""
    updated, _ = sh.bump_pin(CONFIG, "v0.5.3")
    assert "rev: v5.0.0" in updated
    assert updated.count("v0.5.3") == 1


def test_the_rationale_comment_survives_the_bump():
    # The comment is why the pin is a tag at all; a rewrite that drops it deletes
    # the reasoning and invites someone to point it at a branch.
    updated, _ = sh.bump_pin(CONFIG, "v0.5.3")
    assert "rev: v0.5.3 # keep in step with DEVKIT_VERSION" in updated
    assert "must not redden this repo" in updated


def test_a_project_without_a_devkit_pin_is_not_an_error():
    text = "repos:\n  - repo: https://example.com/other\n    rev: v1\n"
    updated, previous = sh.bump_pin(text, "v0.5.3")
    assert previous is None
    assert updated == text


GATE = f"""\
jobs:
  harness:
    steps:
      - uses: actions/checkout@v7
      - name: Check out the shared harness repo
        uses: actions/checkout@v7
        with:
          repository: {sh.DEVKIT_SLUG}
          ref: v0.5.2 # bump with the --pull it corresponds to
          path: .devkit-src
      - name: Check out a vendor fixture
        uses: actions/checkout@v7
        with:
          repository: someone/else
          ref: v1.2.3
"""


def test_the_gate_ref_moves_to_the_pulled_tag():
    updated, previous = sh.bump_gate_ref(GATE, "v0.5.3")
    assert previous == "v0.5.2"
    assert "ref: v0.5.3 # bump with the --pull it corresponds to" in updated


def test_only_devkits_checkout_step_is_retargeted():
    """A workflow checks out several repos. Retargeting the wrong one points a
    third-party checkout at a devkit tag, and CI fails somewhere unrelated."""
    updated, _ = sh.bump_gate_ref(GATE, "v0.5.3")
    assert "ref: v1.2.3" in updated
    assert updated.count("v0.5.3") == 1


def test_a_workflow_without_a_devkit_checkout_is_not_an_error():
    updated, previous = sh.bump_gate_ref("jobs:\n  x:\n    steps: []\n", "v0.5.3")
    assert previous is None
    assert "v0.5.3" not in updated


def test_pull_moves_both_consumer_pins(tmp_path, monkeypatch):
    """RELEASING.md: the `rev:` and the gate's `ref:` are two separate pins and both
    have to land in the same change as the files. Moving one is a half-upgrade."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    _seed(repo, sh.PR_GATE_FILE, GATE)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert sh.read_pin((repo / sh.PRECOMMIT_FILE).read_text()) == "v0.5.3"
    assert "ref: v0.5.3" in (repo / sh.PR_GATE_FILE).read_text()


def test_a_project_with_no_pr_gate_still_pulls(tmp_path, monkeypatch):
    # Not every consumer has a PR gate; its absence is not a failure.
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert not (repo / sh.PR_GATE_FILE).exists()


def test_the_pin_is_read_back_without_its_comment():
    assert sh.read_pin(CONFIG) == "v0.5.2"


def _receipt(root: Path, tag: str = "") -> None:
    sh.write_receipt(root, (), tag=tag)


def test_a_stale_pin_is_detected_from_the_project_alone(tmp_path):
    """No network, no source checkout: both inputs are committed in the project."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _receipt(tmp_path, "v0.5.3")
    assert sh.stale_pin(tmp_path) == ("v0.5.2", "v0.5.3")


def test_a_matching_pin_and_vendored_tag_is_not_stale(tmp_path):
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _receipt(tmp_path, "v0.5.2")
    assert sh.stale_pin(tmp_path) is None


def test_the_sha_stamp_is_never_what_the_pin_is_compared_against(tmp_path):
    """DEVKIT_VERSION holds a SHA by contract, and a SHA never equals a tag, so
    comparing against it would report every project stale forever. Regression for
    the stamp a consumer had to correct by hand."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _seed(tmp_path, sh.VERSION_FILE, "9d95e44\n")
    _receipt(tmp_path, "v0.5.2")
    assert sh.stale_pin(tmp_path) is None


def test_an_unrecorded_tag_reads_as_cannot_tell_not_stale(tmp_path):
    """A pull from before the receipt carried a tag, or an --allow-untagged one.
    A check that cried wolf on every un-upgraded project would be ignored."""
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    _receipt(tmp_path, "")
    assert sh.stale_pin(tmp_path) is None


def test_a_project_missing_either_input_is_not_stale(tmp_path):
    # Pre-adoption, or a project that does not use the published hooks.
    assert sh.stale_pin(tmp_path) is None
    _seed(tmp_path, sh.PRECOMMIT_FILE, CONFIG)
    assert sh.stale_pin(tmp_path) is None


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


# --- the two guards on --pull ------------------------------------------------


def _git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _repo(root: Path, tag: str = "", files: dict[str, str] | None = None) -> Path:
    """A one-commit git repo, optionally tagged. Real git: `describe --exact-match`
    and `status --porcelain` are the behaviours under test, and a fake would only
    assert that the fake works.

    `files` are committed, not left in the tree -- an uncommitted file would make
    the source dirty and trip the other guard, which is the bug this helper had on
    its first outing.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("one")
    for rel, text in (files or {}).items():
        _seed(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    if tag:
        _git(root, "tag", tag)
    return root


def test_a_tagged_clean_source_is_pullable(tmp_path):
    src = _repo(tmp_path / "src", tag="v9.9.9")
    assert sh.source_tag(src) == "v9.9.9"
    assert not sh.source_dirty(src)


def test_an_untagged_source_has_no_tag(tmp_path):
    assert sh.source_tag(_repo(tmp_path / "src")) is None


def test_uncommitted_changes_make_a_source_dirty(tmp_path):
    src = _repo(tmp_path / "src", tag="v9.9.9")
    (src / "f.txt").write_text("changed")
    assert sh.source_dirty(src)


def test_a_non_repo_is_neither_tagged_nor_dirty(tmp_path):
    # Not a git checkout at all: report nothing rather than crashing the gate.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert sh.source_tag(plain) is None
    assert not sh.source_dirty(plain)


def test_pull_refuses_a_dirty_source_without_copying_anything(tmp_path, monkeypatch):
    """The refusal has to come before the copy: a half-upgraded project with a
    matching stamp is the state that cannot be diagnosed afterwards."""
    src = _repo(tmp_path / "src", tag="v9.9.9", files={"scripts/hooks/x.py": "upstream"})
    (src / "f.txt").write_text("now uncommitted")
    repo = tmp_path / "proj"
    _seed(repo, "scripts/hooks/x.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 2
    assert (repo / "scripts/hooks/x.py").read_text() == "old"
    assert not (repo / sh.VERSION_FILE).exists()


def test_pull_refuses_an_untagged_source(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 2
    assert not (repo / "scripts/hooks/x.py").exists()


def test_pull_stamps_the_tag_and_bumps_the_pin_together(tmp_path, monkeypatch):
    """The whole point: three things move as one, or the upgrade is half-done."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src)]) == 0
    assert (repo / "scripts/hooks/x.py").read_text() == "upstream"
    # The stamp is the SHA (its documented contract); the tag lands in the receipt.
    assert re.fullmatch(r"[0-9a-f]{7,40}", (repo / sh.VERSION_FILE).read_text().strip())
    assert sh.read_receipt_tag(repo) == "v0.5.3"
    assert "rev: v0.5.3" in (repo / sh.PRECOMMIT_FILE).read_text()
    assert sh.stale_pin(repo) is None


def test_an_allowed_dirty_pull_is_stamped_provisional(tmp_path, monkeypatch):
    """Marked so it can never be mistaken for a release -- and so `stale_pin`
    keeps reporting it until a real upgrade replaces it."""
    src = _repo(tmp_path / "src", tag="v0.5.3", files={"scripts/hooks/x.py": "upstream"})
    (src / "f.txt").write_text("uncommitted")
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-dirty"]) == 0
    # No tag recorded: these files are at no release, so "cannot tell" is the
    # honest answer rather than naming one they did not come from.
    assert sh.read_receipt_tag(repo) == ""
    assert sh.stale_pin(repo) is None


def test_an_untagged_pull_leaves_the_pin_alone(tmp_path, monkeypatch):
    # There is no tag to move it to; pretending otherwise would pin a nonexistent rev.
    src = _repo(tmp_path / "src", files={"scripts/hooks/x.py": "upstream"})
    repo = tmp_path / "proj"
    _seed(repo, sh.PRECOMMIT_FILE, CONFIG)
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/hooks/x.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert sh.read_pin((repo / sh.PRECOMMIT_FILE).read_text()) == "v0.5.2"


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
    # --allow-untagged throughout the mechanics tests below: `src` is a plain
    # directory, not a checkout, so the release guards have nothing to read. What
    # is under test here is the copy/retire/receipt behaviour, not the guards.
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
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

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old/SKILL.md").exists()
    assert (repo / ".claude/skills/old/state.json").read_text() == "project-owned"


def test_pull_receipt_removes_a_no_longer_managed_unchanged_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0

    _seed(src, "scripts/new.py", "new")
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/new.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / "scripts/old.py").exists()
    assert (repo / "scripts/new.py").read_text() == "new"


def test_pull_receipt_preserves_a_locally_edited_retired_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    _seed(repo, "scripts/old.py", "local edit")

    monkeypatch.setattr(sh, "MANIFEST", ())
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / "scripts/old.py").read_text() == "local edit"


# --- retirement has to leave the *directory* gone too -----------------------
# Every retired skill that shipped a `.py` had run at least once, so it left a
# `__pycache__/` behind. That husk is gitignored, so it is invisible to `git status`
# and to the drift gate, and it made the bare `rmdir()` fail silently -- carameli
# carried nine empty skill directories for months after the skills were retired.


def test_pull_removes_the_bytecode_cache_left_beside_a_retired_file(tmp_path, monkeypatch):
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/engine.py", "obsolete")
    _seed(repo, ".claude/skills/old/__pycache__/engine.cpython-314.pyc", "bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old").exists()


def test_pull_prunes_a_husk_whose_retired_file_is_already_gone(tmp_path, monkeypatch):
    """The case that actually persisted: the file was deleted by hand (a sweep
    commit), so `retired_present` never matched it and the husk outlived every pull."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/__pycache__/engine.cpython-314.pyc", "bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / ".claude/skills/old").exists()


def test_pull_keeps_a_cache_sitting_beside_project_owned_state(tmp_path, monkeypatch):
    """A surviving sibling means the directory is still someone's. Retirement removes
    the file it reviewed and stops -- it does not get to decide the cache is garbage."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/engine.py", "obsolete")
    _seed(repo, ".claude/skills/old/state.json", "project-owned")
    _seed(repo, ".claude/skills/old/__pycache__/engine.cpython-314.pyc", "bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / ".claude/skills/old/state.json").read_text() == "project-owned"
    assert (repo / ".claude/skills/old/__pycache__/engine.cpython-314.pyc").exists()


def test_pull_keeps_a_cache_holding_more_than_bytecode(tmp_path, monkeypatch):
    """`__pycache__` is only ever regenerated bytecode. Something else in there was
    put there deliberately, and deleting a directory tree on a guess is unrecoverable."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/x.py", "upstream")
    _seed(repo, ".claude/skills/old/engine.py", "obsolete")
    _seed(repo, ".claude/skills/old/__pycache__/notes.md", "not bytecode")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "RETIRED_PATHS", (".claude/skills/old/engine.py",))

    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / ".claude/skills/old/__pycache__/notes.md").read_text() == "not bytecode"


def test_pull_receipt_removal_also_prunes_the_bytecode_cache(tmp_path, monkeypatch):
    """The receipt path unlinks files too, and had the identical bare `rmdir()`."""
    src = tmp_path / "shared"
    repo = tmp_path / "proj"
    _seed(src, "scripts/gone/old.py", "old")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/gone/old.py",))
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    _seed(repo, "scripts/gone/__pycache__/old.cpython-314.pyc", "bytecode")

    monkeypatch.setattr(sh, "MANIFEST", ())
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert not (repo / "scripts/gone").exists()


def test_pruning_leaves_a_directory_that_still_has_live_sources(tmp_path, monkeypatch):
    """Retired paths share parents with live ones -- `scripts/hooks/` holds most of the
    harness. The sweep must not treat a busy directory's cache as abandoned."""
    repo = tmp_path / "proj"
    _seed(repo, "scripts/hooks/live.py", "live")
    _seed(repo, "scripts/hooks/__pycache__/live.cpython-314.pyc", "bytecode")

    sh._prune_dir(repo / "scripts/hooks")

    assert (repo / "scripts/hooks/live.py").exists()
    assert (repo / "scripts/hooks/__pycache__/live.cpython-314.pyc").exists()


def test_pruning_a_directory_that_does_not_exist_is_not_an_error(tmp_path):
    sh._prune_dir(tmp_path / "never-existed")


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
    # Untagged, so the stamp falls back to the SHA -- and `stale_pin` will keep
    # reporting it until a tagged pull replaces it.
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert (repo / sh.VERSION_FILE).read_bytes() == b"abc1234\n"


# --- the marker-block tier ----------------------------------------------------
# Vendoring a region of a per-project file. MANIFEST cannot hold `CLAUDE.md` -- every
# project's own prose would read as drift -- so the gated unit is the span between a
# marker pair. What these pin is that an *ungatable* block is never mistaken for a
# clean one: the whole point is that policy nobody is comparing must be loud.

POLICY = "\n## Testing\n\nEvery change ships with tests.\n"


def _host(block_id: str, body: str, before: str = "# Project\n", after: str = "\n# Local\n") -> str:
    begin, end = sh.block_markers(block_id)
    return f"{before}{begin}{body}{end}{after}"


def test_find_block_locates_the_span_between_markers():
    text = _host("engineering", POLICY)
    span, problem = sh.find_block(text, "engineering")
    assert problem == ""
    assert span is not None
    assert text[span[0] : span[1]] == POLICY


def test_find_block_names_each_way_a_block_is_unusable():
    """Each of these is a gate that is not running; the reason reaches the operator
    verbatim, because "no block" and "inverted markers" need different fixes."""
    begin, end = sh.block_markers("eng")
    cases = {
        "# nothing here\n": "no 'eng' block",
        f"{begin}\nbody\n": "'eng' begin marker with no end",
        f"body\n{end}\n": "'eng' end marker with no begin",
        f"{end}\nbody\n{begin}\n": "'eng' markers are inverted",
        f"{begin}a{end}\n{begin}b{end}": "duplicate 'eng' markers",
    }
    for text, expected in cases.items():
        span, problem = sh.find_block(text, "eng")
        assert span is None
        assert problem == expected


def test_extract_block_returns_none_when_unusable():
    assert sh.extract_block(_host("eng", POLICY), "eng") == POLICY
    assert sh.extract_block("# no markers\n", "eng") is None


def test_replace_block_leaves_everything_outside_the_markers_alone():
    """The half of the guarantee the project cares about: devkit owns the region and
    nothing else. A splice that reflowed the host file would make every pull a diff
    nobody asked for."""
    text = _host("eng", "\nold\n", before="# Mine\n\nkeep me\n", after="\n## Also mine\n")
    spliced, problem = sh.replace_block(text, "eng", POLICY)
    assert problem == ""
    assert "keep me" in spliced
    assert "## Also mine" in spliced
    assert "old" not in spliced
    assert sh.extract_block(spliced, "eng") == POLICY


def test_replace_block_reports_instead_of_appending():
    """A host with no markers must not be silently given the policy at some guessed
    offset -- where the block lands in someone's CLAUDE.md is theirs to decide."""
    spliced, problem = sh.replace_block("# No markers\n", "eng", POLICY)
    assert spliced == ""
    assert problem == "no 'eng' block"


def test_replace_block_round_trips_exactly():
    """`--check` runs straight after `--pull` in CI. If a splice normalised so much as
    a trailing newline, the pull would report drift it had just created."""
    text = _host("eng", POLICY)
    spliced, _ = sh.replace_block(text, "eng", sh.extract_block(text, "eng") or "")
    assert spliced == text


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf-host", "crlf-host"])
def test_block_splice_preserves_the_hosts_line_endings(tmp_path, eol):
    """`_read_text`/`_write_text` disable newline translation, so a host keeps exactly
    the endings it had. Both directions are parametrized because the default
    translating pair corrupts a *different* one on each platform, and a single case
    would be a test that never fails on the machine running it:

      - LF host on Windows: the read leaves LF, the write expands it to `os.linesep`
        -- the whole file becomes CRLF.
      - CRLF host on POSIX: the read collapses to LF, the write leaves it -- the whole
        file becomes LF.

    Either way it is a whole-file diff produced by a splice that changed one
    paragraph, on a file the project owns.
    """
    src, repo = tmp_path / "shared", tmp_path / "proj"
    body = eol + b"## Testing" + eol + eol + b"Every change ships with tests." + eol
    for root, prose in ((src, b"# devkit"), (repo, b"# proj" + eol + eol + b"Keep me.")):
        host = root / "CLAUDE.md"
        host.parent.mkdir(parents=True, exist_ok=True)
        stale = body if root is src else eol + b"stale" + eol
        begin, end = (m.encode() for m in sh.block_markers("eng"))
        host.write_bytes(prose + eol + begin + stale + end + eol + b"# Tail" + eol)

    written, failed = sh.sync_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (written, failed) == (["CLAUDE.md#eng"], [])

    landed = (repo / "CLAUDE.md").read_bytes()
    assert landed.count(eol) == landed.count(b"\n"), f"mixed endings in {landed!r}"
    assert (b"\r" in landed) is (eol == b"\r\n")
    assert b"Keep me." in landed
    assert b"stale" not in landed


def _seed_block(root: Path, rel: str, block_id: str, body: str, before: str = "# H\n") -> None:
    _seed(root, rel, _host(block_id, body, before=before))


def test_classify_blocks_partitions_by_region_not_by_file(tmp_path):
    """The whole reason the tier exists: the hosts differ (each project's own prose)
    while the vendored region matches, and that must classify as in sync."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY, before="# devkit\n")
    _seed_block(repo, "CLAUDE.md", "eng", POLICY, before="# carameli\n\nWholly different.\n")
    drifted, unusable, ok = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (drifted, unusable) == ([], [])
    assert ok == ["CLAUDE.md#eng"]


def test_classify_blocks_reports_a_changed_region(tmp_path):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\n## Testing\n\nSometimes.\n")
    drifted, unusable, ok = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert drifted == ["CLAUDE.md#eng"]
    assert (unusable, ok) == ([], [])


def test_classify_blocks_never_counts_an_ungatable_block_as_ok(tmp_path):
    """A missing marker pair means this project is carrying no gated policy at all.
    Silence here would be the failure mode the dispatch-coherence rule exists for."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed(repo, "CLAUDE.md", "# carameli\n\nNo markers at all.\n")
    drifted, unusable, ok = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (drifted, ok) == ([], [])
    assert unusable == ["CLAUDE.md#eng (this project: no 'eng' block)"]


def test_classify_blocks_names_the_side_whose_host_is_absent(tmp_path):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    repo.mkdir(parents=True, exist_ok=True)
    _, unusable, _ = sh.classify_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert unusable == ["CLAUDE.md#eng (CLAUDE.md absent in this project)"]


def test_sync_blocks_writes_the_region_and_keeps_local_prose(tmp_path):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\nstale\n", before="# carameli\n\nMine.\n")
    written, failed = sh.sync_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert (written, failed) == (["CLAUDE.md#eng"], [])
    landed = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert sh.extract_block(landed, "eng") == POLICY
    assert "Mine." in landed
    assert "stale" not in landed


def test_sync_blocks_leaves_the_host_untouched_when_it_cannot_splice(tmp_path):
    """A refusal must not half-write. Same contract as `--pull`'s dirty-source guard."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed(repo, "CLAUDE.md", "# carameli\n\nNo markers.\n")
    written, failed = sh.sync_blocks(src, repo, (("CLAUDE.md", "eng"),))
    assert written == []
    assert failed == ["CLAUDE.md#eng (destination: no 'eng' block)"]
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "# carameli\n\nNo markers.\n"


def test_pull_splices_configured_blocks(tmp_path, monkeypatch):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\nstale\n", before="# proj\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 0
    assert sh.extract_block((repo / "CLAUDE.md").read_text(encoding="utf-8"), "eng") == POLICY


def test_pull_fails_when_a_configured_block_cannot_land(tmp_path, monkeypatch):
    """Exit 1, not a warning: a pull that reports success while the policy stayed
    behind is the half-upgrade the version stamp cannot describe."""
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed(repo, "CLAUDE.md", "# proj\n\nNo markers.\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    monkeypatch.setattr(sh, "git_head", lambda p: "abc1234")
    assert sh.main(["--pull", "--src", str(src), "--allow-untagged"]) == 1
    # The file half still landed, so the receipt describes what is actually on disk.
    assert (repo / "scripts/x.py").exists()


def test_check_fails_on_a_drifted_block(tmp_path, monkeypatch):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY)
    _seed_block(repo, "CLAUDE.md", "eng", "\nreworded locally\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    assert sh.main(["--check", "--src", str(src)]) == 1


def test_check_passes_when_only_the_hosts_differ(tmp_path, monkeypatch):
    src, repo = tmp_path / "shared", tmp_path / "proj"
    _seed(src, "scripts/x.py", "v1")
    _seed(repo, "scripts/x.py", "v1")
    _seed_block(src, "CLAUDE.md", "eng", POLICY, before="# devkit\n")
    _seed_block(repo, "CLAUDE.md", "eng", POLICY, before="# proj\n\nEntirely my own.\n")
    monkeypatch.setattr(sh, "REPO_ROOT", repo)
    monkeypatch.setattr(sh, "MANIFEST", ("scripts/x.py",))
    monkeypatch.setattr(sh, "BLOCK_MANIFEST", (("CLAUDE.md", "eng"),))
    assert sh.main(["--check", "--src", str(src)]) == 0


def test_block_manifest_ships_empty_until_the_content_moves():
    """The tier is inert on arrival by design -- the helpers above are what is under
    test. Delete this the moment a block is configured; it is a scope marker, and
    leaving it would make the migration look like a regression."""
    assert sh.BLOCK_MANIFEST == ()
