from datetime import UTC, date, datetime

import pytest

from _db import make_session_scope
from data_lake.db.models import Instrument, IpoEvent, PriceBar
from data_lake.ingestion.market.listing_dates import (
    ListingDatesReport,
    lower_listed_on,
    resolve_listing_dates,
    tiingo_listed_on,
)
from data_lake.ingestion.market.tiingo import TiingoListing
from data_lake.ingestion.market.yahoo_common import daily_ts


@pytest.fixture
def scope():
    return make_session_scope()


def _listing(ticker, start, exchange="NASDAQ", end=None) -> TiingoListing:
    return TiingoListing(ticker, exchange, "Stock", "USD", start, end)


def _instrument(scope, symbol, first_bar=None, *, exchange="SMART", currency="USD", **extra):
    with scope() as session:
        instrument = Instrument(symbol=symbol, exchange=exchange, currency=currency, **extra)
        session.add(instrument)
        session.flush()
        if first_bar is not None:
            for day in (first_bar, date(2026, 1, 2)):
                session.add(
                    PriceBar(
                        instrument_id=instrument.id,
                        ts=daily_ts(day),
                        bar_size="1 day",
                        source="yahoo",
                        what_to_show="ADJUSTED_LAST",
                        open=1,
                        high=1,
                        low=1,
                        close=1,
                        volume=1,
                    )
                )
        return instrument.id


def _ipo(scope, instrument_id, ipo_date, name="Acme Inc."):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    with scope() as session:
        session.add(
            IpoEvent(
                source="finnhub",
                external_id=name,
                company_name=name,
                stage="priced",
                expected_date=ipo_date,
                instrument_id=instrument_id,
                resolution="yahoo",
                first_seen_at=now,
                fetched_at=now,
            )
        )


def _listed_on(scope, instrument_id) -> date | None:
    with scope() as session:
        instrument = session.get(Instrument, instrument_id)
        assert instrument is not None
        return instrument.listed_on


def test_lower_listed_on_only_moves_earlier():
    instrument = Instrument(symbol="X")
    assert lower_listed_on(instrument, None) is False
    assert lower_listed_on(instrument, date(2020, 5, 1)) is True
    assert lower_listed_on(instrument, date(2021, 1, 1)) is False
    assert lower_listed_on(instrument, date(2020, 5, 1)) is False
    assert instrument.listed_on == date(2020, 5, 1)
    assert lower_listed_on(instrument, date(2019, 1, 1)) is True
    assert instrument.listed_on == date(2019, 1, 1)


def test_tiingo_listed_on_takes_the_latest_start_no_later_than_the_first_bar():
    listings = {
        "ADPT": [_listing("ADPT", date(2014, 6, 25)), _listing("ADPT", date(2019, 6, 27))],
    }
    assert tiingo_listed_on("ADPT", "SMART", date(2020, 3, 2), listings) == date(2019, 6, 27)
    assert tiingo_listed_on("adpt", "SMART", date(2015, 1, 2), listings) == date(2014, 6, 25)


def test_tiingo_listed_on_refuses_a_start_later_than_the_first_bar():
    """LESL listed in 2020, yet Tiingo's file dates the ticker 2026-10-05."""
    listings = {"LESL": [_listing("LESL", date(2026, 10, 5))]}
    assert tiingo_listed_on("LESL", "SMART", date(2020, 10, 29), listings) is None
    assert tiingo_listed_on("NONE", "SMART", date(2020, 10, 29), listings) is None


def test_tiingo_listed_on_matches_a_tiingo_exchange_exactly():
    listings = {
        "MSP": [
            _listing("MSP", date(2005, 4, 27), "NYSE ARCA"),
            _listing("MSP", date(2020, 10, 21), "NYSE"),
        ]
    }
    first_bar = date(2020, 10, 21)
    assert tiingo_listed_on("MSP", "NYSE", first_bar, listings) == date(2020, 10, 21)
    assert tiingo_listed_on("MSP", "nyse arca", first_bar, listings) == date(2005, 4, 27)
    assert tiingo_listed_on("MSP", "PINK", first_bar, listings) is None
    assert tiingo_listed_on("MSP", "TIINGO", first_bar, listings) == date(2020, 10, 21)


def test_an_ipo_sets_the_earlier_of_its_date_and_the_first_bar(scope):
    early_bar = _instrument(scope, "AAA", date(2020, 10, 14))
    _ipo(scope, early_bar, date(2020, 10, 15), "Pricing-day Inc.")
    late_bar = _instrument(scope, "BBB", date(2020, 10, 20))
    _ipo(scope, late_bar, date(2020, 10, 15), "Late-bar Inc.")

    report = resolve_listing_dates(session_factory=scope)

    assert report == ListingDatesReport(from_ipo=2)
    assert _listed_on(scope, early_bar) == date(2020, 10, 14)
    assert _listed_on(scope, late_bar) == date(2020, 10, 15)


def test_tiingo_fills_what_it_safely_can(scope):
    meta = _instrument(scope, "META", date(2014, 1, 2))
    lesl = _instrument(scope, "LESL", date(2020, 10, 29))
    listings = {
        "META": [_listing("META", date(2012, 5, 18))],
        "LESL": [_listing("LESL", date(2026, 10, 5))],
    }

    report = resolve_listing_dates(tiingo_listings=listings, session_factory=scope)

    assert report == ListingDatesReport(from_tiingo=1)
    assert _listed_on(scope, meta) == date(2012, 5, 18)
    assert _listed_on(scope, lesl) is None  # unknown, not wrong


def test_the_first_bar_alone_never_sets_a_listing_date(scope):
    instrument = _instrument(scope, "OLDCO", date(2014, 1, 2))

    assert resolve_listing_dates(tiingo_listings={}, session_factory=scope) == ListingDatesReport()
    assert _listed_on(scope, instrument) is None


def test_a_listing_date_later_than_older_bars_is_lowered_to_them(scope):
    instrument = _instrument(scope, "SPIN", date(2015, 3, 2), listed_on=date(2019, 4, 1))

    report = resolve_listing_dates(session_factory=scope)

    assert report == ListingDatesReport(from_first_bar=1)
    assert _listed_on(scope, instrument) == date(2015, 3, 2)


def test_non_usd_and_fx_instruments_are_not_matched_to_tiingo(scope):
    tsx = _instrument(scope, "SHOP", date(2016, 1, 4), exchange="TSX", currency="CAD")
    fx = _instrument(scope, "EUR", date(2016, 1, 4), exchange="IDEALPRO", sec_type="CASH")
    listings = {
        "SHOP": [_listing("SHOP", date(2015, 5, 21))],
        "EUR": [_listing("EUR", date(2000, 1, 3))],
    }

    assert resolve_listing_dates(tiingo_listings=listings, session_factory=scope) == (
        ListingDatesReport()
    )
    assert _listed_on(scope, tsx) is None
    assert _listed_on(scope, fx) is None


def test_an_ipo_linked_instrument_without_bars_still_gets_its_date(scope):
    instrument = _instrument(scope, "NOBAR")
    _ipo(scope, instrument, date(2024, 2, 1))

    assert resolve_listing_dates(session_factory=scope) == ListingDatesReport(from_ipo=1)
    assert _listed_on(scope, instrument) == date(2024, 2, 1)


def test_a_second_run_changes_nothing(scope):
    instrument = _instrument(scope, "META", date(2014, 1, 2))
    _ipo(scope, instrument, date(2012, 5, 18))
    listings = {"META": [_listing("META", date(2012, 5, 18))]}
    resolve_listing_dates(tiingo_listings=listings, session_factory=scope)

    assert resolve_listing_dates(tiingo_listings=listings, session_factory=scope) == (
        ListingDatesReport()
    )
    assert _listed_on(scope, instrument) == date(2012, 5, 18)
