"""``instruments.listed_on``: when a security first traded — a lower bound, never too late.

The two possible errors are not symmetric. A ``listed_on`` that is too *late* makes an old
company pass for a fresh listing, and a consumer that trains on young listings then learns
from fake young rows. One that is too *early*, or None, only sends the consumer back to its
own fallback — the first stored bar — which is safe. So every source below is used only where
it cannot be later than that fallback, and ``listed_on`` only ever moves earlier.

Sources, and why each is safe:

- **An IPO priced by** ``ipo_pricing``: the earlier of the IPO date and the first bar.
  ``ipo_pricing`` links a series only when its first bar falls within days of the IPO date,
  fetched from weeks before it, so the security demonstrably did not trade before then.
- **Tiingo's** ``supported_tickers`` **startDate**. Tiingo dates a security from its first
  trade under any ticker — ``META`` starts 2012-05-18 (the ``FB`` IPO), ``GEN`` 1990-03-26
  (Symantec), ``ELV`` 2001-10-30 (WellPoint) — but not always: ``LESL`` (Leslie's, listed
  2020-10-29) carries 2026-10-05. All checked against the file on 2026-10-10. So a startDate
  is used only when it is no later than the instrument's first stored bar: the security
  traded on that bar, so a later startDate is wrong, and an earlier one is never worse than
  the fallback. Of several rows for a reused ticker, the latest such start is taken.
- **The first stored bar** is never a source on its own — bars often start where a backfill
  did, not where the security did — but a ``listed_on`` later than it is lowered to it, since
  the security demonstrably traded then. That keeps the invariant when older bars arrive.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select

from data_lake.db.models import Instrument, IpoEvent, PriceBar
from data_lake.ingestion.base import SessionFactory, resolve_session_factory
from data_lake.ingestion.market.tiingo import DEFAULT_EXCHANGE, TiingoListing

__all__ = [
    "ListingDatesReport",
    "lower_listed_on",
    "resolve_listing_dates",
    "tiingo_listed_on",
]

#: Instrument exchanges that do not name a Tiingo exchange: Yahoo's key, and Tiingo's own
#: default when a series was fetched without one.
_ANY_EXCHANGE = frozenset({"SMART", DEFAULT_EXCHANGE})


@dataclass
class ListingDatesReport:
    """Instruments whose ``listed_on`` moved, by the source that moved it."""

    from_ipo: int = 0
    from_tiingo: int = 0
    #: Lowered to the first stored bar, which had come to precede it.
    from_first_bar: int = 0


def lower_listed_on(instrument: Instrument, day: date | None) -> bool:
    """Move ``instrument.listed_on`` to ``day`` if that is earlier (or it is unset)."""
    if day is None or (instrument.listed_on is not None and instrument.listed_on <= day):
        return False
    instrument.listed_on = day
    return True


def tiingo_listed_on(
    symbol: str, exchange: str, first_bar: date, listings: Mapping[str, list[TiingoListing]]
) -> date | None:
    """The latest Tiingo startDate for this ticker that is no later than ``first_bar``.

    An instrument keyed by a Tiingo exchange only matches listings on that exchange.
    """
    starts = [
        row.start
        for row in listings.get(symbol.upper(), [])
        if row.start <= first_bar
        and (exchange.upper() in _ANY_EXCHANGE or row.exchange.upper() == exchange.upper())
    ]
    return max(starts, default=None)


def _day(ts: datetime) -> date:
    return ts.date()


def resolve_listing_dates(
    *,
    tiingo_listings: Mapping[str, list[TiingoListing]] | None = None,
    session_factory: SessionFactory | None = None,
) -> ListingDatesReport:
    """Fill and lower ``listed_on`` for every instrument with stored bars. Idempotent.

    Tiingo is consulted only when ``tiingo_listings`` (``tiingo.parse_supported_tickers``)
    is given; the IPO cohort and the first-bar rule need nothing but the database.
    """
    factory = resolve_session_factory(session_factory)
    report = ListingDatesReport()
    with factory() as session:
        first_bars = {
            instrument_id: _day(first)
            for instrument_id, first in session.execute(
                select(PriceBar.instrument_id, func.min(PriceBar.ts)).group_by(
                    PriceBar.instrument_id
                )
            )
        }
        ipo_dates: dict[int, date] = {}
        for instrument_id, ipo_date in session.execute(
            select(IpoEvent.instrument_id, IpoEvent.expected_date).where(
                IpoEvent.instrument_id.is_not(None), IpoEvent.expected_date.is_not(None)
            )
        ):
            if instrument_id is not None and ipo_date is not None:  # the query's own filter
                ipo_dates[instrument_id] = min(ipo_date, ipo_dates.get(instrument_id, ipo_date))

        for instrument in session.scalars(
            select(Instrument).where(Instrument.id.in_(first_bars.keys() | ipo_dates.keys()))
        ):
            first_bar = first_bars.get(instrument.id)
            ipo_date = ipo_dates.get(instrument.id)
            if ipo_date is not None and lower_listed_on(
                instrument, min(ipo_date, first_bar or ipo_date)
            ):
                report.from_ipo += 1
            if first_bar is None:
                continue
            if (
                tiingo_listings is not None
                and instrument.currency == "USD"
                and instrument.sec_type != "CASH"
                and lower_listed_on(
                    instrument,
                    tiingo_listed_on(
                        instrument.symbol, instrument.exchange, first_bar, tiingo_listings
                    ),
                )
            ):
                report.from_tiingo += 1
            if instrument.listed_on is not None and lower_listed_on(instrument, first_bar):
                report.from_first_bar += 1
    return report
