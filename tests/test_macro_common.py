from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from data_lake.db.models import EconomicEvent, MacroObservation
from data_lake.ingestion.macro import common
from data_lake.ingestion.macro.common import (
    ScheduledEvent,
    TagStackParser,
    delete_vanished_future_events,
    fetch_page,
    has_class,
    newest_observation_date,
    squash,
    upsert_events,
    upsert_observations,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = datetime(2026, 10, 2, tzinfo=UTC)


def _event(external_id: str, day: date, **overrides) -> ScheduledEvent:
    values = {
        "source": "src",
        "external_id": external_id,
        "kind": "data_release",
        "name": "Thing",
        "country": "US",
        "event_date": day,
    }
    values.update(overrides)
    return ScheduledEvent(**values)


def test_observations_upsert_revises_in_place_and_keeps_first_seen():
    session_cm = make_session_scope()
    with session_cm() as session:
        assert newest_observation_date(session, "fred", "X") is None
        points = [(date(2026, 1, 1), 1.0), (date(2026, 2, 1), 2.0), (date(2026, 2, 1), 2.5)]
        assert upsert_observations(session, "fred", "X", points, T0) == 2
        assert upsert_observations(session, "fred", "X", [], T0) == 0
    with session_cm() as session:
        assert upsert_observations(session, "fred", "X", [(date(2026, 2, 1), 3.0)], T1) == 1
        upsert_observations(session, "fred", "Y", [(date(2027, 1, 1), 9.0)], T1)
        assert newest_observation_date(session, "fred", "X") == date(2026, 2, 1)

    with session_cm() as session:
        row = session.scalar(
            select(MacroObservation).where(
                MacroObservation.series_id == "X", MacroObservation.date == date(2026, 2, 1)
            )
        )
    assert row.value == 3.0
    assert row.first_seen_at.replace(tzinfo=UTC) == T0
    assert row.fetched_at.replace(tzinfo=UTC) == T1


def test_events_upsert_refreshes_every_field_but_first_seen():
    session_cm = make_session_scope()
    with session_cm() as session:
        assert upsert_events(session, [_event("a", date(2026, 11, 1))] * 2, T0) == 1
    with session_cm() as session:
        upsert_events(session, [_event("a", date(2026, 11, 1), name="Renamed", raw={"x": 1})], T1)

    with session_cm() as session:
        row = session.scalars(select(EconomicEvent)).one()
    assert (row.name, row.raw) == ("Renamed", {"x": 1})
    assert row.first_seen_at.replace(tzinfo=UTC) == T0
    assert row.fetched_at.replace(tzinfo=UTC) == T1


def test_delete_vanished_future_events_is_scoped_by_source_kind_release_and_date():
    session_cm = make_session_scope()
    today = date(2026, 10, 10)
    with session_cm() as session:
        upsert_events(
            session,
            [
                _event("past", date(2026, 9, 1), release_id=1),
                _event("today", today, release_id=1),
                _event("kept", date(2026, 11, 1), release_id=1),
                _event("gone", date(2026, 11, 2), release_id=1),
                _event("other-release", date(2026, 11, 3), release_id=2),
                _event("other-kind", date(2026, 11, 4), kind="fomc_meeting"),
                _event("other-source", date(2026, 11, 5), source="else"),
            ],
            T0,
        )
    with session_cm() as session:
        deleted = delete_vanished_future_events(
            session, source="src", kind="data_release", keep={"kept"}, today=today, release_id=1
        )
    assert deleted == 1
    with session_cm() as session:
        left = set(session.scalars(select(EconomicEvent.external_id)))
    assert left == {"past", "today", "kept", "other-release", "other-kind", "other-source"}


def test_fetch_page_returns_text_and_wraps_failures(monkeypatch):
    seen = {}

    def ok(url, headers, timeout, follow_redirects):
        seen.update(headers=headers, follow_redirects=follow_redirects)
        return httpx.Response(200, text="<p>hi</p>", request=httpx.Request("GET", url))

    monkeypatch.setattr(common.httpx, "get", ok)
    assert fetch_page("https://example.test/", RuntimeError, "Example") == "<p>hi</p>"
    assert seen == {"headers": {"User-Agent": common.USER_AGENT}, "follow_redirects": True}

    monkeypatch.setattr(
        common.httpx,
        "get",
        lambda url, **kw: httpx.Response(404, request=httpx.Request("GET", url)),
    )
    with pytest.raises(LookupError, match="Example page returned HTTP 404"):
        fetch_page("https://example.test/", LookupError, "Example")


class _Collector(TagStackParser):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[tuple[str, bool]] = []
        self.ended: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.texts.append((data.strip(), self.inside("div", "hit")))

    def on_end(self, tag, attrs) -> None:
        self.ended.append(tag)


def test_tag_stack_parser_tolerates_void_unclosed_and_stray_tags():
    parser = _Collector()
    parser.feed(
        '<div class="a hit"><p>one<br>two<img src="x"/></div></span><div class="miss">three</div>'
    )

    assert parser.texts == [("one", True), ("two", True), ("three", False)]
    assert parser.ended[:2] == ["p", "div"]  # the unclosed <p> closes with its <div>
    assert parser.stack == []


def test_small_text_helpers():
    assert has_class({"class": "row fomc-meeting"}, "fomc-meeting")
    assert not has_class({"class": "fomc-meeting__date"}, "fomc-meeting")
    assert not has_class({}, "x")
    assert squash("  Schedule\n for\N{NO-BREAK SPACE}2026 ") == "Schedule for 2026"
