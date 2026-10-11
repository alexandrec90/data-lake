import logging
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import EarningsEvent, Instrument
from data_lake.ingestion.market import finnhub_earnings as fe
from data_lake.ingestion.market.earnings_common import (
    as_utc,
    delete_vanished_future_earnings,
    upsert_earnings_event,
)

TODAY = date(2026, 10, 10)
KEY = "finnhub-test-key"


def _row(symbol, day, **overrides):
    row = {
        "symbol": symbol,
        "date": day,
        "hour": "amc",
        "year": 2026,
        "quarter": 3,
        "epsEstimate": 1.5,
        "epsActual": None,
        "revenueEstimate": 1_000_000.0,
        "revenueActual": None,
    }
    row.update(overrides)
    return row


class FakeFinnhub:
    def __init__(self, rows=None, status=200) -> None:
        self.rows = rows or []
        self.status = status
        self.calls: list[dict] = []

    def get(self, url, params, headers, timeout):
        assert url == f"{fe.BASE_URL}/calendar/earnings"
        assert headers == {"X-Finnhub-Token": KEY}
        assert "token" not in params
        self.calls.append(dict(params))
        span = [
            r
            for r in self.rows
            if not isinstance(r, dict) or params["from"] <= r["date"] <= params["to"]
        ]
        return httpx.Response(
            self.status, json={"earningsCalendar": span}, request=httpx.Request("GET", url)
        )


@pytest.fixture
def finnhub(monkeypatch):
    monkeypatch.setattr(SETTINGS, "finnhub_key", KEY)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=tz or UTC)

    monkeypatch.setattr(fe, "datetime", FrozenDatetime)
    fake = FakeFinnhub()
    monkeypatch.setattr(fe.httpx, "get", fake.get)
    return fake


def _seeded_scope():
    session_cm = make_session_scope()
    with session_cm() as session:
        session.add_all(
            [
                Instrument(symbol="AAPL", exchange="SMART", currency="USD"),
                Instrument(symbol="MSFT", exchange="SMART", currency="USD"),
                Instrument(symbol="SHOP", exchange="TSX", currency="CAD"),
            ]
        )
    return session_cm


def _events(session_cm) -> list[tuple[str, date]]:
    with session_cm() as session:
        rows = session.execute(
            select(Instrument.symbol, EarningsEvent.report_ts)
            .join(Instrument, Instrument.id == EarningsEvent.instrument_id)
            .order_by(EarningsEvent.report_ts)
        ).all()
    return [(symbol, ts.date()) for symbol, ts in rows]


def test_tracked_instruments_maps_usd_symbols_only():
    session_cm = _seeded_scope()
    with session_cm() as session:
        session.add(Instrument(symbol="AAPL", exchange="NASDAQ", currency="USD"))
    with session_cm() as session:
        tracked = fe.tracked_instruments(session)
    assert set(tracked) == {"AAPL", "MSFT"}
    assert len(tracked["AAPL"]) == 2  # every USD listing of the symbol gets the row


def test_as_utc_reads_naive_values_as_utc():
    naive = datetime(2026, 10, 30, 20, 0)
    assert as_utc(naive) == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    eastern = datetime.fromisoformat("2026-10-30T16:00:00-04:00")
    assert as_utc(eastern) == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)


def test_delete_vanished_future_earnings_respects_its_bounds():
    session_cm = _seeded_scope()
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    stamps = [datetime(2026, 10, d, tzinfo=UTC) for d in (1, 20, 25, 30)]
    with session_cm() as session:
        for ts in stamps:
            upsert_earnings_event(session, 1, ts, "finnhub", {}, now)
            upsert_earnings_event(session, 2, ts, "finnhub", {}, now)
        upsert_earnings_event(session, 1, stamps[1], "yahoo", {}, now)
    with session_cm() as session:
        deleted = delete_vanished_future_earnings(
            session,
            source="finnhub",
            keep={(1, stamps[2])},
            after=now,
            instrument_ids=[1],
            until=datetime(2026, 10, 28, tzinfo=UTC),
        )
    # Only instrument 1's finnhub row on the 20th: past, kept, out-of-window, other
    # instrument and other source all survive.
    assert deleted == 1
    with session_cm() as session:
        left = session.execute(
            select(EarningsEvent.instrument_id, EarningsEvent.source, EarningsEvent.report_ts)
        ).all()
    assert len(left) == 8


def test_spans_cut_a_window_into_bounded_pieces():
    assert fe.spans(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]
    assert fe.spans(date(2026, 1, 1), date(2026, 3, 1), max_days=30) == [
        (date(2026, 1, 1), date(2026, 1, 30)),
        (date(2026, 1, 31), date(2026, 3, 1)),
    ]
    assert fe.spans(date(2026, 1, 2), date(2026, 1, 1)) == []
    with pytest.raises(ValueError):
        fe.spans(date(2026, 1, 1), date(2026, 1, 2), max_days=0)


def test_row_fields_normalises_and_rejects():
    symbol, day, fields = fe.row_fields(_row(" aapl ", "2026-10-30", hour="BMO", epsActual=2))
    assert (symbol, day) == ("AAPL", date(2026, 10, 30))
    assert fields["hour"] == "bmo" and fields["eps_actual"] == 2.0
    assert fe.row_fields(_row("AAPL", "2026-10-30", hour=""))[2]["hour"] is None
    assert fe.row_fields(_row("AAPL", "2026-10-30", epsEstimate=True))[2]["eps_estimate"] is None
    assert fe.row_fields(_row("", "2026-10-30")) is None
    assert fe.row_fields(_row("AAPL", "soon")) is None


def test_fetch_stores_tracked_symbols_only(finnhub, caplog):
    finnhub.rows = [
        _row("AAPL", "2026-10-30", hour="amc"),
        _row("MSFT", "2026-10-06", hour="bmo", epsActual=3.1, revenueActual=2e6),
        _row("SHOP", "2026-10-29"),  # CAD instrument: not a USD match
        _row("ZZZZ", "2026-10-29"),  # never creates an instrument
        _row("AAPL", "2026-10-30", hour="amc"),  # listed twice
        "junk",
    ]
    session_cm = _seeded_scope()
    with caplog.at_level(logging.INFO, logger=fe.__name__):
        count = fe.FinnhubEarningsCalendarConnector(session_factory=session_cm).fetch()

    assert count == 2
    assert "2 skipped" in caplog.text
    with session_cm() as session:
        assert session.scalar(select(Instrument).where(Instrument.symbol == "ZZZZ")) is None
        msft = session.scalar(
            select(EarningsEvent)
            .join(Instrument, Instrument.id == EarningsEvent.instrument_id)
            .where(Instrument.symbol == "MSFT")
        )
    assert msft.source == "finnhub"
    assert msft.report_ts.replace(tzinfo=UTC) == datetime(2026, 10, 6, tzinfo=UTC)
    assert (msft.hour, msft.eps_actual, msft.revenue_actual) == ("bmo", 3.1, 2e6)
    assert msft.first_seen_at is not None and msft.fetched_at is not None


def test_the_window_is_requested_in_month_spans(finnhub):
    fe.FinnhubEarningsCalendarConnector(session_factory=_seeded_scope()).fetch(
        days_back=7, days_ahead=90
    )
    assert [(c["from"], c["to"]) for c in finnhub.calls] == [
        ("2026-10-03", "2026-11-01"),
        ("2026-11-02", "2026-12-01"),
        ("2026-12-02", "2026-12-31"),
        ("2027-01-01", "2027-01-08"),
    ]


def test_refresh_fills_actuals_without_blanking_and_keeps_first_seen(finnhub):
    finnhub.rows = [_row("AAPL", "2026-10-30", epsEstimate=1.5)]
    session_cm = _seeded_scope()
    connector = fe.FinnhubEarningsCalendarConnector(session_factory=session_cm)
    connector.fetch()
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        session.scalars(select(EarningsEvent)).one().first_seen_at = marker

    finnhub.rows = [_row("AAPL", "2026-10-30", epsEstimate=None, epsActual=1.7)]
    connector.fetch()

    with session_cm() as session:
        row = session.scalars(select(EarningsEvent)).one()
    assert (row.eps_estimate, row.eps_actual) == (1.5, 1.7)
    assert row.first_seen_at.replace(tzinfo=UTC) == marker


def test_a_redated_future_report_is_deleted_but_not_the_past(finnhub):
    finnhub.rows = [_row("AAPL", "2026-10-30"), _row("MSFT", "2026-10-06")]
    session_cm = _seeded_scope()
    connector = fe.FinnhubEarningsCalendarConnector(session_factory=session_cm)
    connector.fetch()

    # AAPL re-dated into the next span; MSFT's past row gone. Another company keeps the first
    # span non-empty, as a real month of the calendar always is.
    finnhub.rows = [_row("ZZZZ", "2026-10-20"), _row("AAPL", "2026-11-02")]
    connector.fetch()

    assert _events(session_cm) == [("MSFT", date(2026, 10, 6)), ("AAPL", date(2026, 11, 2))]


def test_rows_beyond_the_window_and_empty_spans_are_left_alone(finnhub):
    finnhub.rows = [_row("AAPL", "2026-10-30"), _row("MSFT", "2026-12-15")]
    session_cm = _seeded_scope()
    fe.FinnhubEarningsCalendarConnector(session_factory=session_cm).fetch(days_ahead=90)

    # A shorter window does not reach MSFT's row, and an all-empty response deletes nothing.
    finnhub.rows = []
    fe.FinnhubEarningsCalendarConnector(session_factory=session_cm).fetch(days_ahead=30)

    assert _events(session_cm) == [("AAPL", date(2026, 10, 30)), ("MSFT", date(2026, 12, 15))]


def test_missing_key_and_bad_arguments(monkeypatch, finnhub):
    with pytest.raises(ValueError):
        fe.FinnhubEarningsCalendarConnector(session_factory=_seeded_scope()).fetch(days_back=-1)
    monkeypatch.setattr(SETTINGS, "finnhub_key", "")
    with pytest.raises(RuntimeError, match="FINNHUB_KEY"):
        fe.FinnhubEarningsCalendarConnector(session_factory=_seeded_scope()).fetch()


def test_http_failures_name_no_key_and_commit_nothing(monkeypatch, finnhub):
    finnhub.status = 429
    session_cm = _seeded_scope()
    with pytest.raises(fe.FinnhubProviderError) as exc_info:
        fe.FinnhubEarningsCalendarConnector(session_factory=session_cm).fetch()
    assert "HTTP 429" in str(exc_info.value) and KEY not in str(exc_info.value)
    assert _events(session_cm) == []

    def broken(url, params, headers, timeout):
        raise httpx.ConnectTimeout("slow")

    monkeypatch.setattr(fe.httpx, "get", broken)
    with pytest.raises(fe.FinnhubProviderError, match="ConnectTimeout"):
        fe.FinnhubEarningsCalendarConnector(session_factory=session_cm).fetch()

    monkeypatch.setattr(
        fe.httpx,
        "get",
        lambda url, params, headers, timeout: httpx.Response(
            200, json={"oops": 1}, request=httpx.Request("GET", url)
        ),
    )
    with pytest.raises(fe.FinnhubProviderError, match="shape"):
        fe.FinnhubEarningsCalendarConnector(session_factory=session_cm).fetch()


@pytest.mark.parametrize("column", ["first_seen_at", "source", "report_ts", "account_id"])
def test_the_shared_upsert_refuses_identity_and_unknown_columns(column):
    now = datetime(2026, 10, 10, tzinfo=UTC)
    with make_session_scope()() as session, pytest.raises(ValueError, match=column):
        upsert_earnings_event(session, 1, now, "finnhub", {column: "x"}, now)
