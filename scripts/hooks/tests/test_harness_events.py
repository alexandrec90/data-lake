"""Tests for harness_events.py -- the central append-only ledger helper.

Project-agnostic on purpose: every path is a tmp_path and the machine seam
($DEVKIT_DIR) is monkeypatched, so the suite passes identically in devkit and in
every consumer it is vendored into.
"""

import datetime as dt

from conftest import load_module

events = load_module("scripts/hooks/harness_events.py")


class TestClean:
    def test_collapses_tabs_and_newlines(self):
        assert events.clean("a\tb\nc  d") == "a b c d"

    def test_empty_becomes_placeholder(self):
        assert events.clean("") == "-"
        assert events.clean("   \n\t ") == "-"

    def test_truncates_long_values(self):
        long = "x" * (events.VALUE_LIMIT * 2)
        assert events.clean(long) == "x" * events.VALUE_LIMIT

    def test_non_string_values_are_stringified(self):
        assert events.clean(42) == "42"

    def test_an_explicit_limit_wins(self):
        assert events.clean("x" * 50, limit=10) == "x" * 10


class TestFieldLimits:
    """A report's proposed fix is at the *end* of it, so a flat cap deletes exactly
    the half a triager needs. `message` and `detail` get their own ceiling."""

    def test_a_reports_message_keeps_its_proposed_fix(self):
        assert events.limit_for("message") > events.VALUE_LIMIT

    def test_a_spawn_failures_detail_keeps_gits_reason(self):
        assert events.limit_for("detail") > events.VALUE_LIMIT

    def test_every_other_field_takes_the_flat_cap(self):
        assert events.limit_for("command") == events.VALUE_LIMIT
        assert events.limit_for("project") == events.VALUE_LIMIT

    def test_a_long_message_survives_into_the_line(self):
        # The exact shape that lost its recommendation: a diagnosis longer than the
        # flat cap, with the fix in its last sentence.
        report = "d" * events.VALUE_LIMIT + " Slug the branch from the box name instead."
        line = events.event_line("s", "agent-report", (("message", report),))
        assert line.endswith("Slug the branch from the box name instead.")

    def test_a_long_message_is_still_bounded(self):
        report = "d" * (events.limit_for("message") * 2)
        line = events.event_line("s", "agent-report", (("message", report),))
        assert line == "s\tevent=agent-report\tmessage=" + "d" * events.limit_for("message")

    def test_a_long_message_is_still_one_line(self):
        report = "a\nb\t" * 500
        line = events.event_line("s", "agent-report", (("message", report),))
        assert "\n" not in line
        assert line.count("\t") == 2


class TestEventLine:
    def test_format(self):
        line = events.event_line(
            "2026-08-21T00:00:00+00:00", "guard-block", (("a", "x"), ("b", ""))
        )
        assert line == "2026-08-21T00:00:00+00:00\tevent=guard-block\ta=x\tb=-"

    def test_no_fields(self):
        line = events.event_line("s", "e", ())
        assert line == "s\tevent=e"


class TestProjectName:
    """The `project=` field has to name a repo, not whichever directory the writer ran in.

    Three of the four writers recorded `REPO_ROOT.name` directly, and from an ephemeral
    box that is the box directory -- so 28% of this machine's ledger named a project that
    does not exist and never will, one pseudo-project per box. Grouping the backlog by
    project was meaningless until this normalised.
    """

    def test_a_checkout_is_its_own_name(self, tmp_path):
        assert events.project_name(tmp_path / "carameli") == "carameli"

    def test_a_box_reads_as_the_project_it_was_cut_from(self, tmp_path):
        box = tmp_path / f"carameli{events.BOX_NAME_SEP}some-task-0824"
        assert events.project_name(box) == "carameli"

    def test_a_hyphenated_project_survives_the_split(self, tmp_path):
        """`--` is the separator, so a single hyphen in a repo name is not one."""
        assert events.project_name(tmp_path / "data-lake") == "data-lake"
        assert (
            events.project_name(tmp_path / f"data-lake{events.BOX_NAME_SEP}fix-0824") == "data-lake"
        )

    def test_a_name_that_is_only_the_separator_falls_back_to_itself(self, tmp_path):
        """Never return an empty string: the ledger field would read as a missing value."""
        assert events.project_name(tmp_path / events.BOX_NAME_SEP) == events.BOX_NAME_SEP


class TestLedgerPath:
    def test_explicit_root_wins(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEVKIT_DIR", raising=False)
        assert events.ledger_path(tmp_path) == tmp_path / "logs" / "harness-events.log"

    def test_devkit_dir_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path))
        assert events.ledger_path() == tmp_path / "logs" / "harness-events.log"

    def test_unset_means_no_ledger(self, monkeypatch):
        monkeypatch.delenv("DEVKIT_DIR", raising=False)
        assert events.ledger_path() is None

    def test_blank_env_means_no_ledger(self, monkeypatch):
        monkeypatch.setenv("DEVKIT_DIR", "   ")
        assert events.ledger_path() is None

    def test_env_pointing_nowhere_means_no_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEVKIT_DIR", str(tmp_path / "missing"))
        assert events.ledger_path() is None


class TestRecord:
    def test_appends_one_line_per_call(self, tmp_path):
        path = events.record("guard-block", (("project", "p"),), root=tmp_path)
        events.record("guard-block", (("project", "q"),), root=tmp_path)
        assert path == tmp_path / "logs" / "harness-events.log"
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0].endswith("\tevent=guard-block\tproject=p")
        assert lines[1].endswith("\tevent=guard-block\tproject=q")

    def test_stamp_is_parseable_iso(self, tmp_path):
        path = events.record("agent-report", (), root=tmp_path)
        stamp = path.read_text(encoding="utf-8").split("\t", 1)[0]
        parsed = dt.datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None

    def test_no_ledger_is_a_silent_noop(self, monkeypatch):
        monkeypatch.delenv("DEVKIT_DIR", raising=False)
        assert events.record("agent-report", (("k", "v"),)) is None

    def test_unwritable_root_never_raises(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where logs/ needs a directory", encoding="utf-8")
        assert events.record("agent-report", (), root=blocker) is None
