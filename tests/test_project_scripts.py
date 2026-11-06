"""Tests for the contract entrypoints the shared workspace tasks dispatch to.

`scripts/run-tests.py` and `scripts/lint-all.py` are what devkit's `devkit_project.py`
invokes for this checkout, and what the vendored Stop hook calls before letting a session
end. They are per-project by design — devkit ships a template, and every consumer's copy
diverges on how it reaches its interpreter and what it lints — so the parts worth pinning
are the ones a plausible edit would break silently.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"


def subprocess_stdout(text: str) -> SimpleNamespace:
    """A finished `git` call that printed `text`."""
    return SimpleNamespace(returncode=0, stdout=text, stderr="")


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


@pytest.mark.parametrize("name", ["run-tests.py", "lint-all.py", "e2e.py"])
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


def test_help_says_it_takes_test_files(capsys):
    """devkit ledger b8b42ded: argparse's own usage line omits what reaches pytest
    unparsed, and a fixer told to pass test files read it as "accepts no test paths"."""
    script = load_script("run-tests.py")
    with pytest.raises(SystemExit):
        script.main(["--help"])
    assert "[--changed] [test file or pytest argument ...]" in capsys.readouterr().out


def test_the_pr_gate_uploads_every_runners_failure_log():
    """devkit ledger 669db913: the gate ran both runners and uploaded neither log, so the
    fix pass's evidence ended at "details in logs/test-failures.log" and its fixer re-ran
    the suite to learn which test had failed."""
    gate = (REPO_ROOT / ".github" / "workflows" / "pr-gate.yml").read_text(encoding="utf-8")
    upload = gate[gate.index("actions/upload-artifact") :]
    assert "if: failure()" in gate[: gate.index("actions/upload-artifact")].rsplit("- name:", 1)[1]
    for log in ("logs/lint-errors.log", "logs/test-failures.log", "logs/junit-hooks.xml"):
        assert log in upload
    assert "--junit-xml=logs/junit-hooks.xml" in gate


def test_no_tests_collected_is_not_a_failure():
    """`stop.py` calls this with the changed files under tests/. Editing a conftest or a
    support module collects nothing, and reporting that as a failure blocks the stop with
    "no tests ran" — which no source edit can resolve."""
    script = load_script("run-tests.py")
    assert script.PYTEST_NO_TESTS_COLLECTED == 5


# --- e2e --------------------------------------------------------------------


JUNIT_ROW = '<testcase classname="tests.test_x" name="test_{name}">{body}</testcase>'


def junit(*rows: str) -> str:
    return f'<testsuites><testsuite name="pytest">{"".join(rows)}</testsuite></testsuites>'


def passed(name: str = "ok") -> str:
    return JUNIT_ROW.format(name=name, body="")


def ended(tag: str, name: str, message: str) -> str:
    return JUNIT_ROW.format(name=name, body=f'<{tag} message="{message}">detail</{tag}>')


@pytest.fixture
def e2e(monkeypatch, tmp_path) -> ModuleType:
    """The script, reporting into a temp tree rather than this checkout's `logs/`."""
    script = load_script("e2e.py")
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "REPORT", tmp_path / "logs" / "e2e-junit.xml")
    return script


def run_e2e(script, report: str | None, code: int = 0, env=None, argv=()) -> tuple[int, list]:
    """`main` over a fake pytest that writes `report`; `(exit code, argvs it was given)`."""
    seen = []

    def runner(cmd, **kwargs):
        seen.append(cmd)
        if report is not None:
            script.REPORT.write_text(report, encoding="utf-8")
        return SimpleNamespace(returncode=code, stdout="pytest said\n", stderr="")

    return script.main(list(argv), runner=runner, env=env or {}), seen


def test_the_tier_is_deselected_by_default_and_selected_by_its_own_run():
    """The default run must stay the no-network suite CLAUDE.md promises, and the tier's
    `-m` must really replace the one in `addopts`: pytest is the only witness to both."""
    for module in ("pyarrow", "duckdb", "botocore.exceptions"):
        pytest.importorskip(module)
    target = "tests/test_e2e_pool.py"
    base = [
        sys.executable,
        "-m",
        "pytest",
        target,
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    default = subprocess.run(base, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    assert default.returncode == 5, default.stdout + default.stderr
    tier = subprocess.run(
        [*base, "-m", "local"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    assert tier.returncode == 0, tier.stdout + tier.stderr
    assert "test_bars_round_trip_through_the_pool_or_stay_put" in tier.stdout


def test_the_tier_installs_both_extras_and_leaves_the_lock_alone():
    """Without the extras `importorskip` drops the whole tier, and the nightly job refuses
    a checkout with a tracked edit, which a rewritten `uv.lock` would be."""
    argv = load_script("e2e.py").pytest_argv(Path("report.xml"))
    assert argv[:7] == ["uv", "run", "--all-extras", "--frozen", "python", "-m", "pytest"]
    assert argv[argv.index("-m", 7) + 1] == "local"
    assert "--junitxml=report.xml" in argv


def test_named_tests_replace_the_tree_but_stay_in_the_tier():
    argv = load_script("e2e.py").pytest_argv(Path("r.xml"), ["tests/test_x.py::test_y"])
    assert "tests/test_x.py::test_y" in argv and "tests" not in argv
    assert argv[argv.index("-m", 7) + 1] == "local"


@pytest.mark.parametrize(
    ("classname", "name", "ident"),
    [
        ("tests.test_x", "test_y", "tests/test_x.py::test_y"),
        ("tests.test_x.TestZ", "test_y", "tests/test_x.py::TestZ::test_y"),
        ("", "tests.test_x", "tests.test_x"),
    ],
)
def test_ids_read_as_pytests_summary_reads_them(classname, name, ident):
    """The id is what the fix pass groups by; a collection error has no classname."""
    assert load_script("e2e.py").pytest_id(classname, name) == ident


def test_an_unreadable_report_is_no_cases(e2e, tmp_path):
    assert e2e.cases(tmp_path / "absent.xml") == []
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><oops", encoding="utf-8")
    assert e2e.cases(broken) == []


def test_a_green_run_exits_zero_and_prints_no_failure(e2e, capsys):
    code, _ = run_e2e(e2e, junit(passed()))
    assert code == 0
    assert "FAILED" not in capsys.readouterr().out


def test_a_failure_exits_with_pytests_code_and_names_the_test(e2e, capsys):
    code, _ = run_e2e(e2e, junit(passed(), ended("failure", "bad", "boom")), code=1)
    assert code == 1
    assert "FAILED tests/test_x.py::test_bad - boom" in capsys.readouterr().out


def test_a_fixture_error_is_printed_as_failed(e2e, capsys):
    """pytest's own line says `ERROR`, and the signature is read from `FAILED` lines, so
    a pool that stopped answering would be signed by an exit code alone."""
    run_e2e(e2e, junit(ended("error", "down", "connection refused")), code=1)
    assert "FAILED tests/test_x.py::test_down - connection refused" in capsys.readouterr().out


def test_a_skip_is_listed_by_hand_and_fails_under_the_scheduler(e2e, capsys):
    report = junit(passed(), ended("skipped", "away", "no pool"))
    assert run_e2e(e2e, report)[0] == 0
    assert "SKIPPED tests/test_x.py::test_away - skipped: no pool" in capsys.readouterr().out

    assert run_e2e(e2e, report, env={e2e.ENV_FLAG: "1"})[0] == 1
    assert "FAILED tests/test_x.py::test_away - skipped: no pool" in capsys.readouterr().out


@pytest.mark.parametrize(
    "report",
    [junit(), junit(ended("skipped", "away", "no pool")), None],
    ids=["no cases", "every case skipped", "no report written"],
)
def test_a_run_that_executed_nothing_is_red_even_by_hand(e2e, capsys, report):
    """A green from zero tests is a skip reported as a pass."""
    assert run_e2e(e2e, report, code=0)[0] == 1
    assert "not a pass" in capsys.readouterr().out


def test_pytests_own_failure_code_survives_a_run_that_executed_nothing(e2e):
    """A usage error or an interrupted run exits 2 or 4, which says more than a bare 1."""
    assert e2e.verdict(4, [], strict=False)[0] == 4
    skipped = e2e.Case("tests/test_x.py::test_y", e2e.SKIPPED, "no pool")
    assert e2e.verdict(2, [skipped], strict=True)[0] == 2


def test_a_stale_report_cannot_answer_for_this_run(e2e):
    """Left in place, last night's green would read as tonight's for a pytest that died
    before it wrote one."""
    e2e.REPORT.parent.mkdir(parents=True)
    e2e.REPORT.write_text(junit(passed()), encoding="utf-8")
    assert run_e2e(e2e, None, code=2)[0] == 2


def test_output_that_a_legacy_console_cannot_encode_still_prints(e2e, capsys):
    """A by-hand run on cp1252 must not die printing the traceback it exists to show."""
    assert e2e._printable("caf\N{LATIN SMALL LETTER E WITH ACUTE}") == "caf\\xe9"
    assert run_e2e(e2e, junit(ended("failure", "bad", "\N{RIGHTWARDS ARROW}")), code=1)[0] == 1
    assert "FAILED tests/test_x.py::test_bad - \\u2192" in capsys.readouterr().out


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
    monkeypatch.setattr(script, "changed_paths", lambda: ["src/data_lake/runtime.py"])
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


def _in_a_repo_with_the_hook(script, monkeypatch, tmp_path, config="- id: detect-secrets\n"):
    """`script` rooted at `tmp_path`, whose commit stage runs `config`; returns its calls."""
    (tmp_path / ".pre-commit-config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(script, "ARTIFACT", tmp_path / "logs" / "lint-errors.log")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        script, "run_check", lambda name, args: (calls.append(args), (name, 0, ""))[1]
    )
    return calls


def test_secrets_targets_are_every_file_only_where_the_hook_is_declared(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    assert script.secrets_targets(["a.md"], skip=False) == [], "no config, nothing to mirror"
    config = tmp_path / ".pre-commit-config.yaml"
    config.write_text("- id: ruff\n", encoding="utf-8")
    assert script.secrets_targets(["a.md"], skip=False) == []
    config.write_text("      - id: detect-secrets\n        exclude: x\n", encoding="utf-8")
    assert script.secrets_targets(["a.md", "b.py"], skip=False) == ["a.md", "b.py"]
    assert script.secrets_targets(["a.md"], skip=True) == []
    assert script.secrets_targets([], skip=False) == []


def test_a_narrowed_run_scans_a_file_no_other_pass_reads(monkeypatch, tmp_path):
    """This runner passed what the commit then refused, a whole dispatch later: the only
    check before `/ship` that reads a non-Python file is the one that refused it."""
    script = load_script("lint-all.py")
    calls = _in_a_repo_with_the_hook(script, monkeypatch, tmp_path)
    (tmp_path / "notes.md").write_text("words\n", encoding="utf-8")

    assert script.main(["--paths", "notes.md"]) == 0
    assert calls == [["pre_commit", "run", "detect-secrets", "--files", "notes.md"]]


def test_a_refused_scan_fails_the_run_and_names_the_hook(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    _in_a_repo_with_the_hook(script, monkeypatch, tmp_path)
    (tmp_path / "notes.md").write_text("words\n", encoding="utf-8")
    monkeypatch.setattr(script, "run_check", lambda name, _args: (name, 1, "Potential secrets"))

    assert script.main(["--changed", "--paths", "notes.md"]) == 1
    log = script.ARTIFACT.read_text(encoding="utf-8")
    assert "=== detect-secrets ===" in log and "Potential secrets" in log


def test_no_secrets_skips_the_scan_and_a_whole_run_leaves_it_to_ci(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    calls = _in_a_repo_with_the_hook(script, monkeypatch, tmp_path)
    (tmp_path / "notes.md").write_text("words\n", encoding="utf-8")

    assert script.main(["--paths", "notes.md", "--no-secrets"]) == 0
    assert calls == []
    assert script.main([]) == 0
    assert all(call[0] != "pre_commit" for call in calls)


def test_a_python_path_still_runs_the_python_checks_beside_the_scan(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    calls = _in_a_repo_with_the_hook(script, monkeypatch, tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert script.main(["--paths", "a.py"]) == 0
    assert [call[0] for call in calls] == ["ruff", "ruff", "mypy", "pre_commit"]


def test_a_deleted_path_is_dropped_rather_than_handed_to_ruff(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    (tmp_path / "kept.py").write_text("x = 1\n", encoding="utf-8")
    assert script.explicit_paths(["kept.py", "gone.py"]) == ["kept.py"]


def test_changed_paths_lists_every_type_and_python_targets_narrows_it(monkeypatch, tmp_path):
    script = load_script("lint-all.py")
    monkeypatch.setattr(script, "REPO_ROOT", tmp_path)
    for name in ("a.py", "b.md"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    listed = subprocess_stdout("a.py\nb.md\ngone.py\n")
    monkeypatch.setattr(script.subprocess, "run", lambda *_a, **_k: listed)
    assert script.changed_paths() == ["a.py", "b.md"]
    assert script.python_targets(["a.py", "b.md", "scripts/hooks/c.py"]) == ["a.py"]


# --- notify-wrap ------------------------------------------------------------


class _Completed:
    """Stands in for `subprocess.CompletedProcess`; only the exit code is read."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.fixture
def notify_wrap(monkeypatch):
    """The wrapper with its toast captured, so a test never reaches win11toast."""
    script = load_script("notify-wrap.py")
    toasts: list[tuple[str, str]] = []
    monkeypatch.setattr(script, "notify", lambda title, message: toasts.append((title, message)))
    script.toasts = toasts
    return script


def run_wrapped(script, monkeypatch, argv, runner):
    monkeypatch.setattr(sys, "argv", ["notify-wrap.py", *argv])
    monkeypatch.setattr(script.subprocess, "run", runner)
    return script.main()


@pytest.mark.parametrize(
    "argv",
    [
        ["Task: Name", "python", "x.py"],  # no `--`, so the command is unidentifiable
        ["--", "python", "x.py"],  # no title
        ["Task: Name", "--"],  # no command
    ],
    ids=["no-separator", "no-title", "no-command"],
)
def test_a_malformed_invocation_exits_2_without_running_anything(notify_wrap, monkeypatch, argv):
    """`2` rather than `1`: a tasks.json typo is a usage error, not a task failure, and
    running a half-parsed argv would launch something nobody asked for."""

    def never(*args, **kwargs):
        raise AssertionError("a malformed invocation must not spawn a process")

    assert run_wrapped(notify_wrap, monkeypatch, argv, never) == 2
    assert notify_wrap.toasts == []


def test_the_wrapped_exit_code_is_passed_through(notify_wrap, monkeypatch):
    """VS Code decides the task's green check / red X from this. Returning 0 on a failing
    suite would paint every broken run as passing."""
    seen: list[list[str]] = []

    def runner(cmd, **kwargs):
        seen.append(cmd)
        return _Completed(3)

    assert run_wrapped(notify_wrap, monkeypatch, ["Test: Suite", "--", "pytest", "-q"], runner) == 3
    assert seen == [["pytest", "-q"]]


def test_a_multi_word_title_survives_the_separator(notify_wrap, monkeypatch):
    """tasks.json passes the label unquoted, so the title arrives as several argv items;
    joining only the first would toast "Test:" for every task."""
    argv = ["Test:", "Run", "pytest", "--", "pytest"]
    assert run_wrapped(notify_wrap, monkeypatch, argv, lambda cmd, **kw: _Completed(0)) == 0
    assert notify_wrap.toasts == [("Test: Run pytest", "Passed in 0s")]


def test_a_failure_toasts_as_a_failure(notify_wrap, monkeypatch):
    run_wrapped(notify_wrap, monkeypatch, ["Lint", "--", "ruff"], lambda cmd, **kw: _Completed(1))
    title, message = notify_wrap.toasts[0]
    assert (title, message.split(" (")[0]) == ("Lint", "Failed")


def test_a_batch_launcher_falls_back_to_the_shell(notify_wrap, monkeypatch):
    """Windows cannot CreateProcess a `.cmd` shim (npm, npx, vite) directly. Without the
    retry the wrapper dies with FileNotFoundError and the task never runs at all."""
    attempts: list[bool] = []

    def runner(cmd, **kwargs):
        attempts.append(kwargs.get("shell", False))
        if len(attempts) == 1:
            raise FileNotFoundError(cmd[0])
        return _Completed(0)

    assert run_wrapped(notify_wrap, monkeypatch, ["Build", "--", "npm", "run"], runner) == 0
    assert attempts == [False, True]
