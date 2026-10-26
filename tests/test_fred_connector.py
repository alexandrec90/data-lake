import logging
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import EconomicEvent, MacroObservation
from data_lake.ingestion.base import AllItemsFailedError
from data_lake.ingestion.macro import fred

KEY = "fred-test-key-not-real"


class NoWait:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


class FakeFred:
    """Answers by path; a handler gets the request params and returns (status, json)."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params, timeout):
        path = url.removeprefix(fred.BASE_URL + "/")
        self.calls.append((path, dict(params)))
        handler = self.handlers.get(path)
        status, body = handler(params) if callable(handler) else (404, {"error_message": "nope"})
        return httpx.Response(status, json=body, request=httpx.Request("GET", url, params=params))

    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(SETTINGS, "fred_api_key", KEY)
    fake = FakeFred()
    monkeypatch.setattr(fred.httpx, "get", fake.get)
    return fake


def _freeze_today(monkeypatch, day: date) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(day.year, day.month, day.day, 12, tzinfo=tz or UTC)

    monkeypatch.setattr(fred, "datetime", FrozenDatetime)


def _observations(*pairs, count=None):
    rows = [{"realtime_start": "2026-10-10", "date": d, "value": v} for d, v in pairs]
    return 200, {"count": len(rows) if count is None else count, "observations": rows}


def _series(session_cm, series_id):
    with session_cm() as session:
        return {
            r.date: r
            for r in session.scalars(
                select(MacroObservation).where(MacroObservation.series_id == series_id)
            )
        }


def test_parsers_skip_missing_and_malformed_rows():
    rows = [
        {"date": "2026-01-01", "value": "1.5"},
        {"date": "2026-02-01", "value": "."},
        {"date": "2026-03-01", "value": "NaN"},
        {"date": "nope", "value": "2"},
        "junk",
    ]
    assert fred.parse_observations(rows) == [(date(2026, 1, 1), 1.5)]
    assert fred.parse_release_dates(
        [{"date": "2026-02-01"}, {"date": "2026-01-01"}, {"date": "2026-01-01"}, {"date": "x"}, 3]
    ) == [date(2026, 1, 1), date(2026, 2, 1)]


def test_a_missing_key_fails_loudly(monkeypatch):
    monkeypatch.setattr(SETTINGS, "fred_api_key", "  ")
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        fred.FredSeriesConnector(session_factory=make_session_scope()).fetch(["CPIAUCSL"])
    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        fred.FredReleaseCalendarConnector(session_factory=make_session_scope()).fetch([10])


def test_an_empty_item_list_is_rejected(api):
    with pytest.raises(ValueError, match="series_ids"):
        fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch([" "])
    with pytest.raises(ValueError, match="release_ids"):
        fred.FredReleaseCalendarConnector(
            session_factory=make_session_scope(), pacer=NoWait()
        ).fetch([])


def test_series_first_run_is_full_history_then_incremental_with_overlap(api):
    api.handlers["series/observations"] = lambda p: _observations(
        ("2026-07-01", "3.1"), ("2026-08-01", "."), ("2026-09-01", "3.0")
    )
    session_cm = make_session_scope()
    pacer = NoWait()
    connector = fred.FredSeriesConnector(session_factory=session_cm, pacer=pacer)

    assert connector.fetch(["cpiaucsl"]) == 2
    path, params = api.calls[-1]
    assert params["series_id"] == "CPIAUCSL"
    assert params["api_key"] == KEY and params["file_type"] == "json"
    assert "observation_start" not in params
    assert pacer.waits == 1
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        for row in session.scalars(select(MacroObservation)):
            row.first_seen_at = marker

    api.handlers["series/observations"] = lambda p: _observations(("2026-09-01", "2.9"))
    assert connector.fetch(["CPIAUCSL"]) == 1
    assert api.calls[-1][1]["observation_start"] == "2026-08-02"  # newest minus 30 days

    rows = _series(session_cm, "CPIAUCSL")
    assert set(rows) == {date(2026, 7, 1), date(2026, 9, 1)}
    assert rows[date(2026, 9, 1)].value == 2.9  # revision overwrites
    assert rows[date(2026, 9, 1)].first_seen_at.replace(tzinfo=UTC) == marker
    assert rows[date(2026, 9, 1)].source == "fred"


def test_series_explicit_since_and_paging(api, monkeypatch):
    monkeypatch.setattr(fred, "OBSERVATIONS_PAGE", 2)
    pages = {
        0: [("2020-01-01", "1"), ("2020-02-01", "2")],
        2: [("2020-03-01", "3")],
    }
    api.handlers["series/observations"] = lambda p: _observations(*pages[p["offset"]], count=3)

    count = fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch(
        ["GDP"], since=date(2020, 1, 1)
    )

    assert count == 3
    assert [(p["offset"], p["limit"]) for _, p in api.calls] == [(0, 2), (2, 2)]
    assert api.calls[0][1]["observation_start"] == "2020-01-01"


def test_one_failing_series_does_not_cost_the_others(api, caplog):
    def handler(params):
        if params["series_id"] == "BAD":
            return 400, {"error_code": 400, "error_message": "Bad Request. Series does not exist."}
        return _observations(("2026-01-01", "1"))

    api.handlers["series/observations"] = handler

    count = fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch(
        ["BAD", "UNRATE"]
    )

    assert count == 1
    assert "BAD" in caplog.text and "Series does not exist" in caplog.text
    assert KEY not in caplog.text


def test_every_series_failing_raises_without_the_key(api):
    api.handlers["series/observations"] = lambda p: (
        400,
        {"error_message": f"The value for variable api_key={KEY} is not registered ({KEY})"},
    )

    with pytest.raises(AllItemsFailedError) as exc_info:
        fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch(
            ["A", "B"]
        )

    message = str(exc_info.value)
    assert "A, B" in message and "HTTP 400" in message and "REDACTED" in message
    assert KEY not in message
    assert KEY not in str(exc_info.value.__cause__)
    assert isinstance(exc_info.value.__cause__, fred.FredProviderError)
    assert exc_info.value.__cause__.__cause__ is None  # no httpx error (and URL) chained


def test_transport_and_body_errors_never_quote_the_url(api, monkeypatch):
    def broken(url, params, timeout):
        raise httpx.ConnectError(f"cannot reach {url}?api_key={params['api_key']}")

    monkeypatch.setattr(fred.httpx, "get", broken)
    with pytest.raises(AllItemsFailedError) as exc_info:
        fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch(["A"])
    assert "ConnectError" in str(exc_info.value)
    assert KEY not in str(exc_info.value)
    assert exc_info.value.__cause__.__suppress_context__ is True

    monkeypatch.setattr(
        fred.httpx,
        "get",
        lambda url, params, timeout: httpx.Response(
            200, text="<html>", request=httpx.Request("GET", url)
        ),
    )
    with pytest.raises(AllItemsFailedError, match="non-JSON"):
        fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch(["A"])

    monkeypatch.setattr(
        fred.httpx,
        "get",
        lambda url, params, timeout: httpx.Response(
            200, json={"observations": "x"}, request=httpx.Request("GET", url)
        ),
    )
    with pytest.raises(AllItemsFailedError, match="shape"):
        fred.FredSeriesConnector(session_factory=make_session_scope(), pacer=NoWait()).fetch(["A"])


def test_httpx_request_logs_are_redacted(caplog):
    fred.install_key_redaction()
    fred.install_key_redaction()  # idempotent
    httpx_logger = logging.getLogger("httpx")
    assert sum(isinstance(f, fred.RedactApiKey) for f in httpx_logger.filters) == 1

    with caplog.at_level(logging.INFO, logger="httpx"):
        httpx_logger.info(
            'HTTP Request: %s %s "%s"',
            "GET",
            f"https://api.stlouisfed.org/fred/series/observations?series_id=X&api_key={KEY}&file_type=json",
            "HTTP/1.1 200 OK",
        )

    assert KEY not in caplog.text
    assert "api_key=REDACTED&file_type=json" in caplog.text


def _release_handlers(api, dates_by_release, names=None):
    names = names or {10: "Consumer Price Index", 50: "Employment Situation"}

    def release(params):
        rid = params["release_id"]
        if rid not in names:
            return 200, {"releases": []}
        return 200, {"releases": [{"id": rid, "name": names[rid]}]}

    def release_dates(params):
        rows = [
            {"release_id": params["release_id"], "date": d}
            for d in dates_by_release[params["release_id"]]
        ]
        return 200, {"count": len(rows), "release_dates": rows}

    api.handlers["release"] = release
    api.handlers["release/dates"] = release_dates


def _calendar(session_cm) -> dict[str, EconomicEvent]:
    with session_cm() as session:
        return {row.external_id: row for row in session.scalars(select(EconomicEvent))}


def test_release_calendar_backfills_then_rereads_a_month(api, monkeypatch):
    _freeze_today(monkeypatch, date(2026, 10, 10))
    _release_handlers(api, {10: ["2026-09-11", "2026-10-15", "2026-11-13"]})
    session_cm = make_session_scope()
    connector = fred.FredReleaseCalendarConnector(session_factory=session_cm, pacer=NoWait())

    assert connector.fetch([10]) == 3
    params = api.calls[-1][1]
    assert params["realtime_start"] == "2000-01-01"
    assert params["realtime_end"] == "9999-12-31"
    assert params["include_release_dates_with_no_data"] == "true"
    assert params["sort_order"] == "asc"

    events = _calendar(session_cm)
    cpi = events["10:2026-10-15"]
    assert (cpi.source, cpi.kind, cpi.country, cpi.release_id) == ("fred", "data_release", "US", 10)
    assert cpi.name == "Consumer Price Index"
    assert cpi.event_date == date(2026, 10, 15)

    connector.fetch([10])
    assert api.calls[-1][1]["realtime_start"] == "2026-09-10"


def test_a_rescheduled_future_release_date_is_deleted(api, monkeypatch):
    _freeze_today(monkeypatch, date(2026, 10, 10))
    dates = {10: ["2026-09-11", "2026-10-15", "2026-11-13"], 50: ["2026-11-06"]}
    _release_handlers(api, dates)
    session_cm = make_session_scope()
    connector = fred.FredReleaseCalendarConnector(session_factory=session_cm, pacer=NoWait())
    connector.fetch([10, 50])

    # CPI moves from Oct 15 to Oct 24; the past Sep 11 date falls outside the window.
    dates[10] = ["2026-10-24", "2026-11-13"]
    connector.fetch([10])

    events = _calendar(session_cm)
    assert "10:2026-10-15" not in events
    assert {"10:2026-10-24", "10:2026-09-11", "50:2026-11-06"} <= set(events)


def test_a_release_listing_no_future_date_deletes_nothing(api, monkeypatch):
    _freeze_today(monkeypatch, date(2026, 10, 10))
    dates = {10: ["2026-10-15"]}
    _release_handlers(api, dates)
    session_cm = make_session_scope()
    connector = fred.FredReleaseCalendarConnector(session_factory=session_cm, pacer=NoWait())
    connector.fetch([10])

    dates[10] = ["2026-09-11"]
    connector.fetch([10])

    assert {"10:2026-10-15", "10:2026-09-11"} <= set(_calendar(session_cm))


def test_a_release_without_a_name_gets_a_fallback_and_failures_are_per_release(
    api, monkeypatch, caplog
):
    _freeze_today(monkeypatch, date(2026, 10, 10))
    _release_handlers(api, {99: ["2026-10-20"], 10: []}, names={10: "Consumer Price Index"})
    original = api.handlers["release/dates"]
    api.handlers["release/dates"] = lambda p: (500, {}) if p["release_id"] == 10 else original(p)
    session_cm = make_session_scope()

    assert (
        fred.FredReleaseCalendarConnector(session_factory=session_cm, pacer=NoWait()).fetch(
            [10, 99]
        )
        == 1
    )
    assert _calendar(session_cm)["99:2026-10-20"].name == "FRED release 99"
    assert "FRED release 10 failed" in caplog.text
