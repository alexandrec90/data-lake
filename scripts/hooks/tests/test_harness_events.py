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


class TestEventLine:
    def test_format(self):
        line = events.event_line(
            "2026-08-21T00:00:00+00:00", "guard-block", (("a", "x"), ("b", ""))
        )
        assert line == "2026-08-21T00:00:00+00:00\tevent=guard-block\ta=x\tb=-"

    def test_no_fields(self):
        line = events.event_line("s", "e", ())
        assert line == "s\tevent=e"


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
