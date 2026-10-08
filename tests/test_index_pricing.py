from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import select
from yfinance.exceptions import YFTzMissingError

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import IndexMembership, Instrument, PriceBar
from data_lake.ingestion.market import index_pricing, tiingo, yahoo, yahoo_common
from data_lake.ingestion.market.index_pricing import (
    PricingOptions,
    covers,
    resolve_index_prices,
)
from data_lake.ingestion.market.tiingo import TiingoListing

SINCE = date(2020, 1, 1)
TODAY = date(2024, 6, 30)
OPTIONS = PricingOptions(today=TODAY)


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(yahoo_common, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(yahoo_common, "_last_request_monotonic", None)


@pytest.fixture
def scope():
    return make_session_scope()


def _span(scope, symbol, start, end=None, **pricing) -> int:
    with scope() as session:
        row = IndexMembership(
            index_code="SP500",
            symbol=symbol,
            start_date=start,
            end_date=end,
            source="fja05680",
            **pricing,
        )
        session.add(row)
        session.flush()
        return row.id


def _get(scope, span_id) -> IndexMembership:
    with scope() as session:
        row = session.get(IndexMembership, span_id)
        assert row is not None
        return row


def _frame(*days: date) -> pd.DataFrame:
    n = len(days)
    return pd.DataFrame(
        {
            "Open": [1.0] * n,
            "High": [1.0] * n,
            "Low": [1.0] * n,
            "Close": [1.0] * n,
            "Volume": [1.0] * n,
        },
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days], tz="America/New_York"),
    )


def _yahoo(monkeypatch, frames: dict[str, object]) -> list[tuple]:
    """Stub the Yahoo download: a DataFrame is returned, an exception raised."""
    calls: list[tuple] = []

    def fake(symbol, start, end, auto_adjust):
        calls.append((symbol, start, end, auto_adjust))
        result = frames.get(symbol, pd.DataFrame())
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(yahoo, "_download_history", fake)
    return calls


def _tiingo(monkeypatch, scope, rows_by_ticker: dict[str, object], usage=None):
    monkeypatch.setattr(SETTINGS, "tiingo_api_key", "k")
    calls: list[str] = []

    def fake(ticker, params, token):
        calls.append(ticker)
        result = rows_by_ticker.get(ticker, 404)
        request = httpx.Request("GET", "https://api.tiingo.com/x")
        if isinstance(result, int):
            return httpx.Response(result, request=request)
        days = result
        return httpx.Response(
            200,
            json=[{"date": f"{d.isoformat()}T00:00:00.000Z", "adjClose": 2.0} for d in days],
            request=request,
        )

    monkeypatch.setattr(tiingo, "_get_prices", fake)
    return tiingo.TiingoConnector(session_factory=scope, usage=usage), calls


def _listing(ticker, start, end=None, exchange="NYSE", asset_type="Stock") -> TiingoListing:
    return TiingoListing(ticker, exchange, asset_type, "USD", start, end)


def test_covers_allows_a_month_late_start_and_ten_day_early_end():
    window, need_to = date(2020, 1, 1), date(2023, 3, 15)
    assert covers(date(2020, 1, 31), date(2023, 3, 6), window, need_to)
    assert not covers(date(2020, 2, 2), date(2023, 3, 15), window, need_to)
    assert not covers(date(2019, 1, 1), date(2023, 3, 4), window, need_to)


def test_existing_yahoo_bars_resolve_without_a_network_call(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    with scope() as session:
        instrument = Instrument(symbol="AAA", exchange="SMART", currency="USD")
        session.add(instrument)
        session.flush()
        for day in (date(2019, 12, 31), date(2024, 6, 25)):
            session.add(
                PriceBar(
                    instrument_id=instrument.id,
                    ts=yahoo_common.daily_ts(day),
                    bar_size="1 day",
                    source="yahoo",
                    what_to_show="ADJUSTED_LAST",
                    open=1,
                    high=1,
                    low=1,
                    close=1,
                )
            )
        instrument_id = instrument.id
    calls = _yahoo(monkeypatch, {})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert calls == []
    assert report.already == 1
    row = _get(scope, span_id)
    assert (row.instrument_id, row.resolution) == (instrument_id, "yahoo")
    assert row.resolved_at is not None


def test_yahoo_download_that_covers_is_stored_under_the_dashed_symbol(monkeypatch, scope):
    span_id = _span(scope, "BRK.B", date(2010, 2, 16))
    calls = _yahoo(monkeypatch, {"BRK-B": _frame(date(2018, 11, 28), date(2024, 6, 28))})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert report.resolved_yahoo == 1
    assert calls == [("BRK-B", SINCE - timedelta(days=400), TODAY + timedelta(days=1), True)]
    row = _get(scope, span_id)
    assert row.resolution == "yahoo"
    with scope() as session:
        instrument = session.get(Instrument, row.instrument_id)
        assert instrument is not None
        assert (instrument.symbol, instrument.exchange) == ("BRK-B", "SMART")
        assert len(session.scalars(select(PriceBar)).all()) == 2


def test_yahoo_download_that_does_not_cover_creates_no_instrument(monkeypatch, scope):
    span_id = _span(scope, "DEAD", date(2012, 1, 1), date(2023, 1, 10))
    # a reused ticker: Yahoo serves the new company, whose history starts too late
    _yahoo(monkeypatch, {"DEAD": _frame(date(2022, 6, 1), date(2024, 6, 28))})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert (report.unpriced, report.resolved_yahoo) == (1, 0)
    row = _get(scope, span_id)
    assert (row.instrument_id, row.resolution) == (None, "unpriced")
    assert row.resolved_at is not None
    with scope() as session:
        assert session.scalars(select(Instrument)).all() == []
        assert session.scalars(select(PriceBar)).all() == []


def test_delisted_yahoo_ticker_falls_through_to_the_tiingo_q_ticker(monkeypatch, scope):
    span_id = _span(scope, "SIVB", date(2018, 3, 19), date(2023, 3, 15))
    _yahoo(monkeypatch, {"SIVB": YFTzMissingError("SIVB")})
    connector, calls = _tiingo(monkeypatch, scope, {"SIVBQ": [date(2018, 11, 1), date(2023, 3, 9)]})
    listings = {"SIVBQ": [_listing("SIVBQ", date(1987, 3, 26), exchange="PINK")]}

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings=listings,
        session_factory=scope,
    )

    assert report.resolved_tiingo == 1
    assert report.failed == {}
    assert calls == ["SIVBQ"]  # SIVB itself is not listed, so never requested
    row = _get(scope, span_id)
    assert row.resolution == "tiingo:SIVBQ"
    with scope() as session:
        instrument = session.get(Instrument, row.instrument_id)
        assert instrument is not None
        assert (instrument.symbol, instrument.exchange) == ("SIVBQ", "PINK")


def test_tiingo_series_that_does_not_cover_leaves_the_span_unpriced(monkeypatch, scope):
    span_id = _span(scope, "GAP", date(2015, 1, 1), date(2023, 3, 15))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {"GAP": [date(2022, 1, 3)]})

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings={"GAP": [_listing("GAP", date(2000, 1, 1), date(2023, 3, 14))]},
        session_factory=scope,
    )

    assert report.unpriced == 1
    assert calls == ["GAP"]
    assert _get(scope, span_id).resolution == "unpriced"


@pytest.mark.parametrize(
    "listings",
    [
        # reused ticker: two companies, no telling which one the API serves
        {
            "DO": [
                _listing("DO", date(1995, 10, 11), date(2022, 11, 30)),
                _listing("DO", date(2022, 12, 1), date(2024, 9, 3)),
            ]
        },
        {"DO": [_listing("DO", date(1995, 10, 11), asset_type="ETF")]},
        {"DO": [_listing("DO", date(2021, 1, 1))]},  # listing starts too late for the span
    ],
    ids=["reused", "not-a-stock", "listing-too-short"],
)
def test_unusable_tiingo_listings_are_never_requested(monkeypatch, scope, listings):
    _span(scope, "DO", date(2009, 1, 1), date(2022, 11, 30))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {})

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings=listings,
        session_factory=scope,
    )

    assert calls == []
    assert report.unpriced == 1


def test_tiingo_budget_exhaustion_defers_instead_of_marking_unpriced(monkeypatch, scope):
    first = _span(scope, "AAA", date(2015, 1, 1), date(2023, 1, 1))
    second = _span(scope, "BBB", date(2015, 1, 1), date(2023, 1, 1))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {}, usage=tiingo.TiingoUsage(monthly_symbols=0))
    listings = {s: [_listing(s, date(2000, 1, 1))] for s in ("AAA", "BBB")}

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings=listings,
        session_factory=scope,
    )

    assert (report.deferred, report.unpriced) == (2, 0)
    assert calls == []
    assert _get(scope, first).resolution is None
    assert _get(scope, second).resolution is None


def test_tiingo_429_defers_the_rest_of_the_run(monkeypatch, scope):
    _span(scope, "AAA", date(2015, 1, 1), date(2023, 1, 1))
    _span(scope, "BBB", date(2015, 1, 1), date(2023, 1, 1))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {"AAA": 429, "BBB": 429})
    listings = {s: [_listing(s, date(2000, 1, 1))] for s in ("AAA", "BBB")}

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings=listings,
        session_factory=scope,
    )

    assert calls == ["AAA"]
    assert report.deferred == 2


def test_refused_tiingo_key_fails_one_span_and_defers_the_rest(monkeypatch, scope):
    _span(scope, "AAA", date(2015, 1, 1), date(2023, 1, 1))
    _span(scope, "BBB", date(2015, 1, 1), date(2023, 1, 1))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {"AAA": 401, "BBB": 401})
    listings = {s: [_listing(s, date(2000, 1, 1))] for s in ("AAA", "BBB")}

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings=listings,
        session_factory=scope,
    )

    assert calls == ["AAA"]
    assert list(report.failed) == ["AAA@2015-01-01"]
    assert report.deferred == 1


def test_unpriced_spans_are_retried_only_after_the_window(monkeypatch, scope):
    now = datetime.now(tz=UTC)
    _span(
        scope,
        "RECENT",
        date(2015, 1, 1),
        date(2023, 1, 1),
        resolution="unpriced",
        resolved_at=now - timedelta(days=5),
    )
    _span(
        scope,
        "STALE",
        date(2015, 1, 1),
        date(2023, 1, 1),
        resolution="unpriced",
        resolved_at=now - timedelta(days=40),
    )
    calls = _yahoo(monkeypatch, {})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert [c[0] for c in calls] == ["STALE"]
    assert report.unpriced == 1


def test_only_unresolved_spans_live_since_the_cutoff_are_considered(monkeypatch, scope):
    with scope() as session:
        instrument = Instrument(symbol="X", exchange="SMART", currency="USD")
        session.add(instrument)
        session.flush()
        instrument_id = instrument.id
    _span(scope, "ENDED", date(2001, 1, 1), date(2019, 12, 31))
    _span(scope, "PRICED", date(2001, 1, 1), resolution="yahoo", instrument_id=instrument_id)
    _span(scope, "DONE", date(2001, 1, 1), resolution="yahoo")
    with scope() as session:
        session.add(
            IndexMembership(
                index_code="NDX", symbol="OTHER", start_date=date(2001, 1, 1), source="x"
            )
        )
    calls = _yahoo(monkeypatch, {})

    resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    # DONE has a resolution but no instrument, so it is still unresolved
    assert [c[0] for c in calls] == ["DONE"]


def test_options_pick_the_index_and_the_warmup(monkeypatch, scope):
    _span(scope, "AAA", date(2015, 1, 1))
    with scope() as session:
        session.add(
            IndexMembership(index_code="NDX", symbol="NNN", start_date=date(2015, 1, 1), source="x")
        )
    calls = _yahoo(monkeypatch, {})
    options = PricingOptions(index_code="NDX", today=TODAY, warmup_days=10)

    resolve_index_prices(since=SINCE, options=options, session_factory=scope)

    assert calls == [("NNN", SINCE - timedelta(days=10), TODAY + timedelta(days=1), True)]


def test_yahoo_probe_budget_defers_the_overflow(monkeypatch, scope):
    _span(scope, "AAA", date(2015, 1, 1))
    _span(scope, "BBB", date(2015, 1, 1))
    calls = _yahoo(monkeypatch, {"AAA": _frame(date(2018, 1, 2), date(2024, 6, 28))})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, max_yahoo=1, session_factory=scope)

    assert [c[0] for c in calls] == ["AAA"]
    assert (report.resolved_yahoo, report.deferred) == (1, 1)


def test_a_failing_span_is_recorded_and_the_run_continues(monkeypatch, scope):
    _span(scope, "AAA", date(2015, 1, 1))
    bad = _span(scope, "BBB", date(2015, 1, 1))
    _yahoo(
        monkeypatch,
        {
            "AAA": _frame(date(2018, 1, 2), date(2024, 6, 28)),
            "BBB": RuntimeError("rate limited"),
        },
    )

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert report.resolved_yahoo == 1
    assert list(report.failed) == ["BBB@2015-01-01"]
    assert "rate limited" in report.failed["BBB@2015-01-01"]
    assert _get(scope, bad).resolution is None  # left for the next run


def test_every_attempted_span_failing_raises(monkeypatch, scope):
    _span(scope, "AAA", date(2015, 1, 1))
    _span(scope, "BBB", date(2015, 1, 1))
    _yahoo(monkeypatch, {"AAA": RuntimeError("down"), "BBB": RuntimeError("down")})

    with pytest.raises(RuntimeError, match="all 2 span"):
        resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)


def test_a_quiet_run_does_not_raise(scope):
    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)
    assert report == index_pricing.PricingReport()
