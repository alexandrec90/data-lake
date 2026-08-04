"""Tests for the contract entrypoints the shared workspace tasks dispatch to.

`scripts/run-tests.py` and `scripts/lint-all.py` are what devkit's `devkit_project.py`
invokes for this checkout, and what the vendored Stop hook calls before letting a session
end. They are per-project by design — devkit ships a template, and every consumer's copy
diverges on how it reaches its interpreter and what it lints — so the parts worth pinning
are the ones a plausible edit would break silently.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"


def load_script(name: str) -> ModuleType:
    """Import a kebab-case script by path; it is not an importable module name."""
    path = SCRIPT_DIR / name
    module_name = f"_test_script_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --- the contract itself ----------------------------------------------------


@pytest.mark.parametrize("name", ["run-tests.py", "lint-all.py"])
def test_the_contract_entrypoints_exist(name):
    """devkit addresses these BY PATH with the checkout as cwd, so a rename turns a
    one-click workspace task into a missing-script error."""
    assert (SCRIPT_DIR / name).is_file()


# --- run-tests --------------------------------------------------------------


def test_tests_run_through_uv_not_the_calling_interpreter():
    """devkit's template uses `sys.executable`, which is right for a project whose venv
    is already active. Here the caller is a VS Code task or a hook launched with the
    desktop's PATH, and a bare interpreter dies on the first `data_lake` import."""
    script = load_script("run-tests.py")
    assert script.build_command(changed=False, extra=[])[:5] == [
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
    ]


def test_changed_selects_pytests_last_failed_subset():
    script = load_script("run-tests.py")
    cmd = script.build_command(changed=True, extra=[])
    assert "--last-failed" in cmd
    # Without this, a --changed run with nothing previously failing runs the WHOLE
    # suite, which is the opposite of what the caller asked for.
    assert "--last-failed-no-failures" in cmd and "all" in cmd


def test_empty_extra_arguments_are_dropped():
    """A VS Code picker can yield "", which argparse would read as a stray positional."""
    script = load_script("run-tests.py")
    assert script.build_command(changed=False, extra=["", "-k", ""]).count("") == 0


def test_no_tests_collected_is_not_a_failure():
    """`stop.py` calls this with the changed files under tests/. Editing a conftest or a
    support module collects nothing, and reporting that as a failure blocks the stop with
    "no tests ran" — which no source edit can resolve."""
    script = load_script("run-tests.py")
    assert script.PYTEST_NO_TESTS_COLLECTED == 5


def test_third_party_frames_are_stripped_from_the_artifact():
    """An agent cannot fix a frame inside site-packages, and a long SQLAlchemy traceback
    buries the one first-party frame that matters."""
    script = load_script("run-tests.py")
    raw = "\n".join(
        [
            "some passing noise",
            "=== FAILURES ===",
            "src/data_lake/ingestion/base.py:12: in fetch",
            "/usr/lib/python3.12/site-packages/httpx/_client.py:99: in send",
            "E   AssertionError",
        ]
    )
    filtered = script.filter_output(raw)
    assert "site-packages" not in filtered
    assert "some passing noise" not in filtered
    assert "src/data_lake/ingestion/base.py:12: in fetch" in filtered


def test_one_deep_failure_cannot_crowd_out_the_others():
    script = load_script("run-tests.py")
    block = "\n".join(["_____ test_one _____", *[f"line {i}" for i in range(60)]])
    capped = script.cap_failure_blocks(block, limit=10)
    assert "lines total, truncated" in capped
    assert len(capped.splitlines()) < 20


# --- lint-all ---------------------------------------------------------------


def test_lint_runs_through_uv_too():
    script = load_script("lint-all.py")
    assert script.build_command(["ruff", "check", "src"])[:4] == ["uv", "run", "python", "-m"]


def test_the_vendored_tier_is_not_a_lint_target():
    """`scripts/hooks/` is byte-compared against devkit by `sync-devkit.py --check`.
    Formatting it here would rewrite files devkit owns and then report as drift — which
    would look like this project's fault."""
    script = load_script("lint-all.py")
    assert "scripts" not in script.DEFAULT_TARGETS
    assert set(script.DEFAULT_TARGETS) == {"src", "tests"}


def test_changed_never_narrows_mypy(monkeypatch):
    """A partial type check is worse than none: imports pull the package graph back in,
    so a subset run reports errors from files it was not asked about while missing the
    ones it was. This package is a graph of Protocols that resolve against each other."""
    script = load_script("lint-all.py")
    calls: list[list[str]] = []
    monkeypatch.setattr(script, "changed_python_files", lambda: ["src/data_lake/runtime.py"])
    monkeypatch.setattr(
        script,
        "run_check",
        lambda name, module_args: (calls.append(module_args), (name, 0, ""))[1],
    )

    assert script.main(["--changed"]) == 0
    mypy_call = next(c for c in calls if c[0] == "mypy")
    assert mypy_call == ["mypy", "src"], "mypy was narrowed to the diff"


def test_a_clean_run_still_writes_the_artifact(monkeypatch, tmp_path):
    """Written empty on success too — a stale artifact from a previous failure would send
    the next agent chasing something already fixed."""
    script = load_script("lint-all.py")
    artifact = tmp_path / "lint-errors.log"
    artifact.write_text("old failure", encoding="utf-8")
    monkeypatch.setattr(script, "ARTIFACT", artifact)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "run_check", lambda name, _args: (name, 0, ""))

    assert script.main([]) == 0
    assert artifact.read_text(encoding="utf-8") == ""


def test_a_failing_check_names_itself_in_the_artifact(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    artifact = tmp_path / "lint-errors.log"
    monkeypatch.setattr(script, "ARTIFACT", artifact)
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        script,
        "run_check",
        lambda name, _args: (name, 1 if name == "mypy" else 0, "boom" if name == "mypy" else ""),
    )

    assert script.main([]) == 1
    log = artifact.read_text(encoding="utf-8")
    assert "=== mypy ===" in log and "boom" in log
    assert "=== ruff ===" not in log
