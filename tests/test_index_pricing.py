import csv
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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
    MIN_PARTIAL_BARS,
    RENAMES_FILE,
    PricingOptions,
    Rename,
    covers,
    covers_partially,
    final_symbol,
    load_renames,
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


def _weekdays(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return [day for day in days if day.weekday() < 5]


def _store_yahoo_bars(scope, symbol, days) -> int:
    with scope() as session:
        instrument = Instrument(symbol=symbol, exchange="SMART", currency="USD")
        session.add(instrument)
        session.flush()
        for day in days:
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
        return instrument.id


def _renames(*rows: tuple[str, str, date]) -> dict[str, Rename]:
    return {old: Rename(old, new, day, "https://example.test/notice") for old, new, day in rows}


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
        # starts too late even for a partial series: no room for a year of bars
        {"DO": [_listing("DO", date(2022, 6, 1))]},
        # covers the start but stops before the span does
        {"DO": [_listing("DO", date(1995, 10, 11), date(2022, 6, 1))]},
    ],
    ids=["reused", "not-a-stock", "listing-too-short", "listing-ends-early"],
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


# --- partial pricing: a late start is accepted, an early end never is ---

LATE = _weekdays(date(2022, 1, 3), date(2024, 6, 28))  # ~650 bars, reaching TODAY


def test_covers_partially_needs_the_end_and_a_year_of_bars():
    window, need_to = date(2020, 1, 1), date(2024, 6, 30)
    assert covers_partially(date(2022, 1, 3), date(2024, 6, 20), 250, window, need_to)
    assert not covers_partially(date(2022, 1, 3), date(2024, 6, 19), 400, window, need_to)
    assert not covers_partially(date(2022, 1, 3), date(2024, 6, 28), 249, window, need_to)
    # a series that starts in time is `covers`'s case, not a partial one
    assert not covers_partially(date(2020, 1, 31), date(2024, 6, 28), 900, window, need_to)


def test_late_starting_yahoo_series_that_reaches_the_end_is_priced_partially(monkeypatch, scope):
    span_id = _span(scope, "FOXA", date(2004, 12, 20))
    _yahoo(monkeypatch, {"FOXA": _frame(*LATE)})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert (report.resolved_yahoo, report.partial, report.unpriced) == (1, 1, 0)
    row = _get(scope, span_id)
    assert row.resolution == "yahoo:partial:2022-01-03"
    assert row.resolution.split(":")[0] == "yahoo"  # the prefix the consumer groups on
    with scope() as session:
        assert len(session.scalars(select(PriceBar)).all()) == len(LATE)


@pytest.mark.parametrize(
    "days",
    [
        _weekdays(date(2021, 1, 4), date(2023, 3, 31)),  # late start, stops three months early
        _weekdays(date(2018, 1, 2), date(2023, 3, 31)),  # covers the start, stops early
        LATE[-(MIN_PARTIAL_BARS - 1) :],  # reaches the end, one bar short of a year
    ],
    ids=["late-start-early-end", "early-end", "below-minimum"],
)
def test_series_that_misses_the_end_or_the_minimum_is_rejected(monkeypatch, scope, days):
    end = date(2023, 6, 30) if days[-1] < date(2024, 1, 1) else None
    span_id = _span(scope, "AAA", date(2015, 1, 1), end)
    _yahoo(monkeypatch, {"AAA": _frame(*days)})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert (report.unpriced, report.partial) == (1, 0)
    assert _get(scope, span_id).resolution == "unpriced"
    with scope() as session:
        assert session.scalars(select(Instrument)).all() == []


def test_exactly_the_minimum_is_enough(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    days = LATE[-MIN_PARTIAL_BARS:]
    _yahoo(monkeypatch, {"AAA": _frame(*days)})

    resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert _get(scope, span_id).resolution == f"yahoo:partial:{days[0].isoformat()}"


def test_full_tiingo_coverage_is_preferred_over_a_partial_yahoo_series(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1), date(2023, 1, 3))
    _yahoo(monkeypatch, {"AAA": _frame(*_weekdays(date(2021, 1, 4), date(2023, 1, 3)))})
    connector, calls = _tiingo(monkeypatch, scope, {"AAA": [date(2018, 11, 1), date(2022, 12, 30)]})

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings={"AAA": [_listing("AAA", date(2000, 1, 1))]},
        session_factory=scope,
    )

    assert calls == ["AAA"]
    assert (report.resolved_tiingo, report.resolved_yahoo, report.partial) == (1, 0, 0)
    assert _get(scope, span_id).resolution == "tiingo:AAA"
    with scope() as session:  # the partial Yahoo series was never stored
        assert [i.exchange for i in session.scalars(select(Instrument))] == ["NYSE"]


def test_spent_tiingo_budget_defers_a_partial_rather_than_settle_for_it(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    _yahoo(monkeypatch, {"AAA": _frame(*LATE)})
    connector, _ = _tiingo(monkeypatch, scope, {}, usage=tiingo.TiingoUsage(monthly_symbols=0))

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings={"AAA": [_listing("AAA", date(2000, 1, 1))]},
        session_factory=scope,
    )

    assert (report.deferred, report.partial) == (1, 0)
    assert _get(scope, span_id).resolution is None


def test_tiingo_listing_that_starts_late_prices_partially(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1), date(2023, 6, 30))
    _yahoo(monkeypatch, {})
    days = _weekdays(date(2021, 6, 1), date(2023, 6, 30))
    connector, calls = _tiingo(monkeypatch, scope, {"AAA": days})

    report = resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings={"AAA": [_listing("AAA", date(2021, 6, 1))]},
        session_factory=scope,
    )

    assert calls == ["AAA"]
    assert (report.resolved_tiingo, report.partial) == (1, 1)
    assert _get(scope, span_id).resolution == "tiingo:AAA:partial:2021-06-01"


def test_partial_only_tiingo_listing_is_not_fetched_when_yahoo_prices_partially(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    _yahoo(monkeypatch, {"AAA": _frame(*LATE)})
    connector, calls = _tiingo(monkeypatch, scope, {})

    resolve_index_prices(
        since=SINCE,
        options=OPTIONS,
        tiingo=connector,
        tiingo_listings={"AAA": [_listing("AAA", date(2021, 6, 1))]},
        session_factory=scope,
    )

    assert calls == []
    assert _get(scope, span_id).resolution == "yahoo:partial:2022-01-03"


def test_stored_partial_bars_give_way_to_a_full_yahoo_series(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    _store_yahoo_bars(scope, "AAA", LATE)
    calls = _yahoo(monkeypatch, {"AAA": _frame(date(2018, 11, 1), date(2024, 6, 28))})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert [c[0] for c in calls] == ["AAA"]
    assert (report.already, report.resolved_yahoo, report.partial) == (0, 1, 0)
    assert _get(scope, span_id).resolution == "yahoo"


def test_stored_bars_that_fit_neither_way_are_ignored(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    _store_yahoo_bars(scope, "AAA", LATE[:10])  # a few bars, long before the end
    _yahoo(monkeypatch, {})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert (report.already, report.unpriced) == (0, 1)
    assert _get(scope, span_id).resolution == "unpriced"


def test_stored_partial_bars_price_the_span_when_nothing_covers(monkeypatch, scope):
    span_id = _span(scope, "AAA", date(2015, 1, 1))
    instrument_id = _store_yahoo_bars(scope, "AAA", LATE)
    _yahoo(monkeypatch, {})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert (report.already, report.partial) == (1, 1)
    row = _get(scope, span_id)
    assert (row.instrument_id, row.resolution) == (instrument_id, "yahoo:partial:2022-01-03")


# --- renames: the curated table, never a guess ---


def test_renamed_span_is_priced_from_the_new_symbol(monkeypatch, scope):
    old = _span(scope, "FB", date(2013, 12, 23), date(2022, 6, 9))
    new = _span(scope, "META", date(2022, 6, 9))
    calls = _yahoo(monkeypatch, {"META": _frame(date(2012, 5, 18), date(2024, 6, 28))})
    options = PricingOptions(today=TODAY, renames=_renames(("FB", "META", date(2022, 6, 9))))

    report = resolve_index_prices(since=SINCE, options=options, session_factory=scope)

    assert [c[0] for c in calls] == ["META"]  # META's own span reuses the stored bars
    assert (report.resolved_yahoo, report.already, report.renamed) == (1, 1, 1)
    old_row, new_row = _get(scope, old), _get(scope, new)
    assert old_row.resolution == "yahoo:renamed:META"
    assert new_row.resolution == "yahoo"
    assert old_row.instrument_id == new_row.instrument_id is not None


def test_rename_chain_is_followed_to_the_final_symbol(monkeypatch, scope):
    span_id = _span(scope, "SYMC", date(2003, 3, 31), date(2019, 11, 5))
    calls = _yahoo(monkeypatch, {"GEN": _frame(date(1989, 6, 23), date(2024, 6, 28))})
    renames = _renames(
        ("SYMC", "NLOK", date(2019, 11, 5)),
        ("NLOK", "GEN", date(2022, 11, 8)),
    )

    resolve_index_prices(
        since=date(2010, 1, 1),
        options=PricingOptions(today=TODAY, renames=renames),
        session_factory=scope,
    )

    assert [c[0] for c in calls] == ["GEN"]
    assert _get(scope, span_id).resolution == "yahoo:renamed:GEN"


def test_renamed_symbol_whose_history_does_not_cover_stays_unpriced(monkeypatch, scope):
    span_id = _span(scope, "CBS", date(1996, 1, 2), date(2019, 12, 5))
    _yahoo(monkeypatch, {"PARA": _frame(*_weekdays(date(2021, 2, 12), date(2024, 6, 28)))})
    renames = _renames(("CBS", "PARA", date(2019, 12, 5)))

    report = resolve_index_prices(
        since=date(2010, 1, 1),
        options=PricingOptions(today=TODAY, renames=renames),
        session_factory=scope,
    )

    assert (report.unpriced, report.renamed) == (1, 0)
    assert _get(scope, span_id).resolution == "unpriced"
    with scope() as session:
        assert session.scalars(select(Instrument)).all() == []


def test_renamed_span_can_be_priced_partially(monkeypatch, scope):
    span_id = _span(scope, "ARNC", date(1996, 1, 2), date(2020, 4, 6))
    days = _weekdays(date(2016, 11, 1), date(2024, 6, 28))
    _yahoo(monkeypatch, {"HWM": _frame(*days)})
    renames = _renames(("ARNC", "HWM", date(2020, 4, 1)))

    report = resolve_index_prices(
        since=date(2010, 1, 1),
        options=PricingOptions(today=TODAY, renames=renames),
        session_factory=scope,
    )

    assert (report.partial, report.renamed) == (1, 1)
    assert _get(scope, span_id).resolution == "yahoo:renamed:HWM:partial:2016-11-01"


def test_renamed_span_priced_by_tiingo_keeps_the_ticker_and_the_marker(monkeypatch, scope):
    span_id = _span(scope, "DISCA", date(2010, 3, 1), date(2022, 4, 11))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {"WBD": [date(2018, 11, 1), date(2022, 4, 8)]})

    resolve_index_prices(
        since=SINCE,
        options=PricingOptions(today=TODAY, renames=_renames(("DISCA", "WBD", date(2022, 4, 11)))),
        tiingo=connector,
        tiingo_listings={"WBD": [_listing("WBD", date(2005, 9, 19), exchange="NASDAQ")]},
        session_factory=scope,
    )

    assert calls == ["WBD"]
    assert _get(scope, span_id).resolution == "tiingo:WBD:renamed"


def test_span_running_past_the_rename_is_not_mapped(monkeypatch, scope):
    # the old ticker went on naming an index member after the rename: it was reused
    _span(scope, "IR", date(2010, 11, 17))
    calls = _yahoo(monkeypatch, {})
    renames = _renames(("IR", "TT", date(2020, 3, 2)))

    resolve_index_prices(
        since=SINCE, options=PricingOptions(today=TODAY, renames=renames), session_factory=scope
    )

    assert [c[0] for c in calls] == ["IR"]


def test_final_symbol_follows_a_chain_and_refuses_a_loop():
    renames = _renames(("A", "B", date(2019, 1, 1)), ("B", "C", date(2020, 1, 1)))
    assert final_symbol("A", renames) == "C"
    assert final_symbol("Z", renames) == "Z"
    with pytest.raises(ValueError, match="loops"):
        final_symbol("A", {**renames, **_renames(("C", "A", date(2021, 1, 1)))})


# --- the packaged rename table ---

COLUMNS = ["old_symbol", "new_symbol", "effective_date", "source"]


def _raw_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == COLUMNS
        return list(reader)


def test_packaged_rename_table_rows_are_cited_and_unique():
    rows = _raw_rows(RENAMES_FILE)
    assert rows
    assert all(row["source"].strip().startswith("https://") for row in rows)
    old_symbols = [row["old_symbol"] for row in rows]
    assert len(old_symbols) == len(set(old_symbols))


def test_packaged_rename_table_chains_end_and_move_forward_in_time():
    renames = load_renames()  # raises on a cycle
    for rename in renames.values():
        assert final_symbol(rename.old_symbol, renames) not in renames
        follow = renames.get(rename.new_symbol)
        if follow is not None:
            assert follow.effective_date > rename.effective_date, rename


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("AAA,BBB,2020-01-01,\n", "every column"),
        ("AAA,BBB,2020-01-01,https://x\nAAA,CCC,2021-01-01,https://y\n", "twice"),
        ("AAA,AAA,2020-01-01,https://x\n", "itself"),
        ("AAA,BBB,2020-01-01,https://x\nBBB,AAA,2021-01-01,https://y\n", "loops"),
    ],
    ids=["no-source", "duplicate", "self-rename", "cycle"],
)
def test_load_renames_refuses_an_ambiguous_table(tmp_path, body, error):
    path = tmp_path / "renames.csv"
    path.write_text(",".join(COLUMNS) + "\n" + body, encoding="utf-8")
    with pytest.raises(ValueError, match=error):
        load_renames(path)


# --- PSKY: renamed SKYD on 2026-10-06; Yahoo's PSKY daily series shrank to a stub ---

PSKY_TODAY = date(2026, 10, 8)
NO_RENAMES = PricingOptions(today=PSKY_TODAY, renames={})
#: The daily rows with a close that Yahoo served for PSKY on 2026-10-09, for any start date;
#: its monthly series for the same ticker goes back to 1994.
PSKY_YAHOO_STUB = [
    date(2026, 7, 29),
    date(2026, 8, 4),
    date(2026, 8, 7),
    date(2026, 8, 11),
    date(2026, 9, 14),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
    date(2026, 10, 8),
]


def test_series_from_the_first_trading_day_covers_a_span_starting_the_next_day(monkeypatch, scope):
    span_id = _span(scope, "PSKY", date(2025, 8, 8))
    _yahoo(monkeypatch, {"PSKY": _frame(*_weekdays(date(2025, 8, 7), PSKY_TODAY))})

    resolve_index_prices(since=SINCE, options=NO_RENAMES, session_factory=scope)

    assert _get(scope, span_id).resolution == "yahoo"


def test_yahoo_daily_stub_is_neither_full_nor_partial_coverage(monkeypatch, scope):
    span_id = _span(scope, "PSKY", date(2025, 8, 8))
    _yahoo(monkeypatch, {"PSKY": _frame(*PSKY_YAHOO_STUB)})

    report = resolve_index_prices(since=SINCE, options=NO_RENAMES, session_factory=scope)

    assert (report.unpriced, report.partial) == (1, 0)
    assert _get(scope, span_id).resolution == "unpriced"


def test_open_span_just_renamed_is_priced_from_the_packaged_tables_new_ticker(monkeypatch, scope):
    # the index file still shows PSKY open, three days after it began trading as SKYD
    span_id = _span(scope, "PSKY", date(2025, 8, 8))
    calls = _yahoo(
        monkeypatch,
        {
            "PSKY": _frame(*PSKY_YAHOO_STUB),
            "SKYD": _frame(*_weekdays(date(2024, 1, 2), PSKY_TODAY)),
        },
    )

    report = resolve_index_prices(
        since=SINCE, options=PricingOptions(today=PSKY_TODAY), session_factory=scope
    )

    assert [c[0] for c in calls] == ["SKYD"]
    assert report.renamed == 1
    assert _get(scope, span_id).resolution == "yahoo:renamed:SKYD"


def test_open_span_long_after_the_rename_is_not_mapped(monkeypatch, scope):
    _span(scope, "PSKY", date(2025, 8, 8))
    calls = _yahoo(monkeypatch, {})
    late = PSKY_TODAY + index_pricing.RENAME_SLACK

    resolve_index_prices(since=SINCE, options=PricingOptions(today=late), session_factory=scope)

    assert [c[0] for c in calls] == ["PSKY"]


def test_yahoo_daily_stub_falls_through_to_tiingo(monkeypatch, scope):
    span_id = _span(scope, "PSKY", date(2025, 8, 8))
    _yahoo(monkeypatch, {"PSKY": _frame(*PSKY_YAHOO_STUB)})
    connector, calls = _tiingo(monkeypatch, scope, {"PSKY": [date(2025, 8, 7), PSKY_TODAY]})

    report = resolve_index_prices(
        since=SINCE,
        options=NO_RENAMES,
        tiingo=connector,
        tiingo_listings={"PSKY": [_listing("PSKY", date(2025, 8, 7), exchange="NASDAQ")]},
        session_factory=scope,
    )

    assert calls == ["PSKY"]
    assert (report.resolved_tiingo, report.partial) == (1, 0)
    assert _get(scope, span_id).resolution == "tiingo:PSKY"
    with scope() as session:  # the stub was never stored as a Yahoo instrument
        assert [i.symbol for i in session.scalars(select(Instrument))] == ["PSKY"]
        assert [i.exchange for i in session.scalars(select(Instrument))] == ["NASDAQ"]


# --- a ticker another security took over inside the span ---


def test_span_whose_ticker_changed_hands_inside_it_is_left_unpriced(monkeypatch, scope):
    # IR named Ingersoll-Rand plc until 2020-03-02, then Gardner Denver (GDI) took it, and
    # the security's history reaches back through its GDI years
    span_id = _span(scope, "IR", date(2010, 11, 17))
    calls = _yahoo(monkeypatch, {"IR": _frame(*_weekdays(date(2017, 5, 12), date(2024, 6, 28)))})
    renames = _renames(("GDI", "IR", date(2020, 3, 2)))

    report = resolve_index_prices(
        since=SINCE, options=PricingOptions(today=TODAY, renames=renames), session_factory=scope
    )

    assert calls == []
    assert (report.unpriced, report.partial) == (1, 0)
    row = _get(scope, span_id)
    assert (row.instrument_id, row.resolution) == (None, "unpriced:ticker-taken:2020-03-02")


def test_span_that_starts_with_the_hand_off_is_priced(monkeypatch, scope):
    span_id = _span(scope, "IR", date(2020, 3, 3))
    _yahoo(monkeypatch, {"IR": _frame(date(2017, 5, 12), date(2024, 6, 28))})
    renames = _renames(("GDI", "IR", date(2020, 3, 2)))

    resolve_index_prices(
        since=SINCE, options=PricingOptions(today=TODAY, renames=renames), session_factory=scope
    )

    assert _get(scope, span_id).resolution == "yahoo"


def test_ticker_taken_spans_wait_out_the_retry_window(monkeypatch, scope):
    _span(
        scope,
        "IR",
        date(2010, 11, 17),
        resolution="unpriced:ticker-taken:2020-03-02",
        resolved_at=datetime.now(tz=UTC) - timedelta(days=5),
    )
    calls = _yahoo(monkeypatch, {})

    report = resolve_index_prices(since=SINCE, options=OPTIONS, session_factory=scope)

    assert calls == []
    assert report == index_pricing.PricingReport()
