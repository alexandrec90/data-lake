"""Price the IPO cohort: every listing on Finnhub's IPO calendar, dead names included.

The rows are ``ipo_events`` from ``finnhub`` at stage ``priced``: the calendar's completed
deals, each with the ticker it listed under and its first trading day (Finnhub's ``date``
matched the first bar on every deal checked, 2026-10-10). EDGAR's ``424B4`` rows are left
alone — they carry no ticker for a company that has since died, and describe the same deals.

**Only listings of common shares or ADRs are priced.** :func:`exclusion` sets the rest aside
from the row alone, with no provider call: units, warrants, rights and preferreds by their
ticker's suffix, SPACs and funds by name. Finnhub lists a SPAC under its *unit* ticker and
sometimes under the name of the company it later merged with (``SoFi Technologies, Inc.`` as
``IPOEU``), so the unit suffix catches most SPACs. The rest — a SPAC sold as shares alone,
renamed (``Nautilus Biotechnology, Inc.`` as ``ARYA``) — is caught by how it trades: a SPAC's
trust holds it near its $10.00 offer price for months, which a company's first weeks of
trading rarely look like (:func:`trades_like_a_spac`), and is ``excluded:spac-like``. A
mutual bank converting to stock also sells at $10.00 and trades as quietly, and is excluded
with them: losing it costs coverage, while admitting a SPAC would put months of a trust's
flat line into the cohort as a young company. EDGAR's SIC 6770 cannot replace this: the
calendar carries no CIK, and a merged SPAC's filer takes the operating company's SIC.

**Ticker reuse is the trap.** ``ADPT`` was Adeptus Health from 2014 and Adaptive
Biotechnologies from 2019; Yahoo serves only the current holder of a ticker. So a series is
accepted only when it starts within days of the IPO date (``FIRST_BAR_EARLY`` before to
``FIRST_BAR_LATE`` after), having been asked for from ``LOOKBACK`` before it. A later company
under the same ticker starts too late; an earlier one — or a security that traded over the
counter before "listing" — has bars too early. Either way the row is left ``unpriced``,
because pricing a company with another's history is worse than a gap the consumer can see.

Sources, in order:

a. bars already stored — no provider call;
b. Yahoo, budgeted by ``max_yahoo`` — skipped when Tiingo's listing for the deal has ended,
   since Yahoo forgets delisted tickers;
c. Tiingo, inside the ``TiingoUsage`` ledger: a listing of the ticker, or of its bankruptcy
   ``Q`` ticker (Tiingo files a security under its last ticker: Codiak's ``CDAK`` is listed
   only as ``CDAKQ``, starting on the IPO date), whose startDate is near the IPO date;
d. otherwise ``unpriced``, retried after ``retry_unpriced_days``. A ticker renamed after the
   IPO (``SQ`` -> ``XYZ``) lands here: nothing ties the new ticker to the deal.

A priced row also sets ``instruments.listed_on`` (see ``listing_dates``).
"""

import re
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, IpoEvent, PriceBar
from data_lake.ingestion.base import SessionFactory, resolve_session_factory
from data_lake.ingestion.market import yahoo_common
from data_lake.ingestion.market.bars import upsert_daily_bars
from data_lake.ingestion.market.index_pricing import probe_yahoo
from data_lake.ingestion.market.listing_dates import lower_listed_on
from data_lake.ingestion.market.tiingo import (
    TiingoBudgetExhausted,
    TiingoConnector,
    TiingoListing,
    TiingoProviderError,
    find_instrument,
)
from data_lake.ingestion.market.yahoo_common import YahooProviderError

__all__ = [
    "FIRST_BAR_EARLY",
    "FIRST_BAR_LATE",
    "IpoPricingOptions",
    "IpoPricingReport",
    "exclusion",
    "provider_symbol",
    "resolve_ipo_prices",
    "starts_at_listing",
    "trades_like_a_spac",
]

#: The ``ipo_events`` rows this prices.
SOURCE = "finnhub"
STAGE = "priced"

#: How far from the IPO date a series' first bar may fall and still be the IPO's.
FIRST_BAR_EARLY = timedelta(days=3)
FIRST_BAR_LATE = timedelta(days=10)
#: Bars are asked for from this long before the IPO date, so earlier history shows up.
LOOKBACK = timedelta(days=30)
#: Fewer bars than this is a stub, not a listing.
MIN_BARS = 5
#: An IPO younger than this is left for a later run: its first bars may not be served yet.
SETTLE = timedelta(days=14)
#: A Tiingo listing that ended within this of today is still trading.
LIVE_SLACK = timedelta(days=10)

#: A SPAC sells at $10.00 and trades near its trust value until it merges.
SPAC_OFFER_PRICE = 10.0
#: The first bars judged, and the fewest needed to judge at all (about four weeks).
SPAC_WINDOW_BARS = 60
SPAC_MIN_BARS = 20
#: The median absolute daily move at or below which a $10.00 deal trades like a trust.
#: Calibrated 2026-10-10 on the 78 $10.00 calendar deals of 2013-2026 that Yahoo still serves:
#: SPACs measured 0.0000-0.0087, operating companies 0.0091 and up, mutual-bank
#: conversions 0.0026-0.0075 (excluded with the SPACs; see the module docstring).
SPAC_MEDIAN_MOVE = 0.009

#: Ticker suffixes after a separator (``AAC'U``, ``PL.WS``, ``ABR-PD``) or as the fifth
#: letter of a five-letter ticker (``CONXU``: Nasdaq's convention).
_SUFFIX = re.compile(r"^([A-Z0-9]+)[.'\- /=+^]([A-Z]+)$")
_SUFFIX_KIND = {
    "U": "unit",
    "UN": "unit",
    "UT": "unit",
    "W": "warrant",
    "WS": "warrant",
    "WT": "warrant",
    "R": "right",
    "RT": "right",
}
_FIFTH_LETTER_KIND = {"U": "unit", "W": "warrant", "R": "right"}

_SPAC_NAME = re.compile(
    r"\bacquisitions?\b|\bspac\b|special purpose|blank check|\bmerger\s+(?:[ivx\d]+\s+)?corp",
    re.IGNORECASE,
)
_FUND_NAME = re.compile(
    r"\betf\b|\bfunds?\b|\bishares\b|currencyshares|commodity trust|\b(?:gold|silver|platinum"
    r"|palladium|bitcoin|ether(?:eum)?)\s+(?:trust|etf|fund)\b",
    re.IGNORECASE,
)
_PREFERRED_NAME = re.compile(r"\bpreferred\b|\bnotes? due\b|\bdebentures?\b", re.IGNORECASE)


@dataclass
class IpoPricingReport:
    #: Priced from bars already stored, with no provider call.
    already: int = 0
    resolved_yahoo: int = 0
    resolved_tiingo: int = 0
    #: Tried, and no source's series starts at the listing; retried later.
    unpriced: int = 0
    #: Not a listing of common shares or ADRs; ``excluded:<reason>`` by reason.
    excluded: dict[str, int] = field(default_factory=dict)
    #: Left for a later run: the Yahoo budget or Tiingo's is spent, or the IPO is too new.
    deferred: int = 0
    #: ``"SYMBOL@date"`` -> error, for rows whose provider call raised.
    failed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IpoPricingOptions:
    """The knobs a scheduled run leaves at their defaults.

    ``today`` defaults to the current UTC date; unpriced rows are retried after
    ``retry_unpriced_days``. An ``excluded`` row is never retried: clear its ``resolution``
    to have it judged again.
    """

    today: date | None = None
    retry_unpriced_days: int = 30


@dataclass(frozen=True)
class _Row:
    id: int
    symbol: str | None
    name: str
    ipo_date: date
    price_low: float | None
    price_high: float | None

    @property
    def label(self) -> str:
        return f"{self.symbol}@{self.ipo_date.isoformat()}"

    @property
    def spac_priced(self) -> bool:
        return self.price_low == SPAC_OFFER_PRICE and self.price_high == SPAC_OFFER_PRICE


@dataclass(frozen=True)
class _Series:
    """A source's daily closes for one instrument, oldest first, from its first bar on."""

    days: tuple[date, ...]
    closes: tuple[float, ...]

    @classmethod
    def of(cls, values: Sequence[dict[str, Any]]) -> "_Series | None":
        rows = sorted(values, key=lambda value: value["ts"])
        if not rows:
            return None
        return cls(
            tuple(row["ts"].date() for row in rows), tuple(float(row["close"]) for row in rows)
        )


def provider_symbol(symbol: str) -> str:
    """Yahoo and Tiingo spell a class share with a dash (``BRK-B``); calendars use a dot."""
    return symbol.strip().upper().replace(".", "-")


def exclusion(symbol: str | None, name: str) -> str | None:
    """Why a calendar row is not a listing of common shares or ADRs, or None if it is one."""
    if not symbol:
        return "no-symbol"
    symbol = symbol.strip().upper()
    match = _SUFFIX.match(symbol)
    if match:
        suffix = match.group(2)
        if suffix in _SUFFIX_KIND:
            return _SUFFIX_KIND[suffix]
        if suffix.startswith("P"):
            return "preferred"
    elif len(symbol) == 5 and symbol.isalpha() and symbol[-1] in _FIFTH_LETTER_KIND:
        return _FIFTH_LETTER_KIND[symbol[-1]]
    if _SPAC_NAME.search(name):
        return "spac"
    if _FUND_NAME.search(name):
        return "fund"
    if _PREFERRED_NAME.search(name):
        return "preferred"
    return None


def starts_at_listing(first_bar: date, bars: int, ipo_date: date) -> bool:
    """Whether a series whose first bar is ``first_bar`` is the listing made on ``ipo_date``."""
    return ipo_date - FIRST_BAR_EARLY <= first_bar <= ipo_date + FIRST_BAR_LATE and bars >= MIN_BARS


def trades_like_a_spac(closes: Sequence[float]) -> bool | None:
    """Whether a $10.00 deal's first bars sit still the way a SPAC's trust holds them.

    None when there are too few bars to judge. Judged on the median absolute daily move, so
    the odd jump on merger rumours does not hide the weeks pinned to the trust around it.
    """
    window = list(closes[:SPAC_WINDOW_BARS])
    if len(window) < SPAC_MIN_BARS:
        return None
    moves = [abs(b / a - 1) for a, b in zip(window, window[1:], strict=False) if a > 0]
    return statistics.median(moves) <= SPAC_MEDIAN_MOVE


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def _stored_series(session: Session, instrument_id: int, source: str) -> _Series | None:
    rows = session.execute(
        select(PriceBar.ts, PriceBar.close)
        .where(
            PriceBar.instrument_id == instrument_id,
            PriceBar.source == source,
            PriceBar.what_to_show == "ADJUSTED_LAST",
            PriceBar.bar_size == "1 day",
        )
        .order_by(PriceBar.ts)
    ).all()
    return _Series.of([{"ts": ts, "close": close} for ts, close in rows])


def _pending_rows(
    factory: SessionFactory, since: date, today: date, retry_after: datetime
) -> list[_Row]:
    with factory() as session:
        rows = session.scalars(
            select(IpoEvent)
            .where(
                IpoEvent.source == SOURCE,
                IpoEvent.stage == STAGE,
                IpoEvent.instrument_id.is_(None),
                IpoEvent.expected_date >= since,
                IpoEvent.expected_date <= today,
                or_(IpoEvent.resolution.is_(None), IpoEvent.resolution.like("unpriced%")),
            )
            .order_by(IpoEvent.expected_date, IpoEvent.id)
        ).all()
        return [
            _Row(
                row.id,
                row.symbol,
                row.company_name,
                row.expected_date,
                row.price_low,
                row.price_high,
            )
            for row in rows
            if row.expected_date is not None
            and not (row.resolved_at is not None and _aware(row.resolved_at) > retry_after)
        ]


def _mark(
    session: Session, row_id: int, instrument_id: int | None, resolution: str, now: datetime
) -> None:
    row = session.get(IpoEvent, row_id)
    assert row is not None
    row.instrument_id = instrument_id
    row.resolution = resolution
    row.resolved_at = now


@dataclass
class _Run:
    """One run's state: what is left of the Yahoo budget, and whether Tiingo is blocked."""

    factory: SessionFactory
    now: datetime
    today: date
    yahoo_left: int
    tiingo: TiingoConnector | None
    listings: Mapping[str, list[TiingoListing]]
    #: Budget spent or key refused: defer rather than mark unpriced.
    tiingo_blocked: bool = False
    attempted: int = 0
    report: IpoPricingReport = field(default_factory=IpoPricingReport)

    def resolve(self, row: _Row) -> None:
        reason = exclusion(row.symbol, row.name)
        symbol = provider_symbol(row.symbol or "")
        near = self._near_listings(symbol, row.ipo_date)
        if reason is None and near and all(listing.asset_type != "Stock" for listing in near):
            reason = "fund"  # Tiingo files the ticker that listed that day as an ETF or fund
        if reason is not None:
            self._exclude(row, reason)
            return
        if row.ipo_date > self.today - SETTLE:
            self.report.deferred += 1
            return
        stocks = [listing for listing in near if listing.asset_type == "Stock"]
        if self._resolve_stored(row, symbol, stocks):
            return
        try:
            self._resolve_with_providers(row, symbol, stocks)
        except TiingoProviderError as exc:
            if exc.status in (401, 403):
                self.tiingo_blocked = True  # the key will not start working mid-run
            self.report.failed[row.label] = str(exc)
        except YahooProviderError as exc:
            self.report.failed[row.label] = str(exc)

    def raise_if_all_failed(self) -> None:
        failed = self.report.failed
        if self.attempted and len(failed) == self.attempted:
            sample = "; ".join(f"{k}: {v}" for k, v in list(failed.items())[:3])
            raise RuntimeError(f"all {self.attempted} IPO(s) sent to a provider failed ({sample})")

    def _near_listings(self, symbol: str, ipo_date: date) -> list[TiingoListing]:
        """Tiingo listings of the ticker or its ``Q`` ticker that started at the IPO."""
        if not symbol:
            return []
        return [
            listing
            for ticker in (symbol, symbol + "Q")
            for listing in self.listings.get(ticker, [])
            if ipo_date - FIRST_BAR_EARLY <= listing.start <= ipo_date + FIRST_BAR_LATE
        ]

    def _exclude(self, row: _Row, reason: str) -> None:
        with self.factory() as session:
            _mark(session, row.id, None, f"excluded:{reason}", self.now)
        self.report.excluded[reason] = self.report.excluded.get(reason, 0) + 1

    def _unpriced(self, row: _Row) -> None:
        with self.factory() as session:
            _mark(session, row.id, None, "unpriced", self.now)
        self.report.unpriced += 1

    def _judge(self, row: _Row, series: _Series | None) -> str:
        """``"ok"``, ``"no"`` (not this listing), ``"spac"``, or ``"wait"`` (too new to tell)."""
        if series is None or not starts_at_listing(series.days[0], len(series.days), row.ipo_date):
            return "no"
        if row.spac_priced:
            verdict = trades_like_a_spac(series.closes)
            if verdict:
                return "spac"
            if verdict is None and series.days[-1] >= self.today - LIVE_SLACK:
                return "wait"  # still trading, and too few bars yet to rule out a trust
        return "ok"

    def _settle(self, row: _Row, verdict: str) -> bool:
        """Act on a non-``ok`` verdict that ends the row's run; True when it did."""
        if verdict == "spac":
            self._exclude(row, "spac-like")
            return True
        if verdict == "wait":
            self.report.deferred += 1
            return True
        return False

    def _link(
        self, session: Session, row: _Row, instrument: Instrument, resolution: str, first: date
    ) -> None:
        lower_listed_on(instrument, min(row.ipo_date, first))
        _mark(session, row.id, instrument.id, resolution, self.now)

    def _resolve_stored(self, row: _Row, symbol: str, stocks: list[TiingoListing]) -> bool:
        """Price the row from bars already stored, if any source's series starts at it."""
        settled: str | None = None
        with self.factory() as session:
            candidates = [(yahoo_common.get_instrument(session, symbol), "yahoo")] + [
                (find_instrument(session, listing.ticker, listing.exchange), "tiingo")
                for listing in stocks
            ]
            for instrument, source in candidates:
                if instrument is None:
                    continue
                series = _stored_series(session, instrument.id, source)
                verdict = self._judge(row, series)
                if verdict == "ok" and series is not None:
                    resolution = "yahoo" if source == "yahoo" else f"tiingo:{instrument.symbol}"
                    self._link(session, row, instrument, resolution, series.days[0])
                    self.report.already += 1
                    return True
                if verdict != "no":
                    settled = verdict
                    break
        return settled is not None and self._settle(row, settled)

    def _resolve_with_providers(self, row: _Row, symbol: str, stocks: list[TiingoListing]) -> None:
        ended = bool(stocks) and all(
            listing.end is not None and listing.end < self.today - LIVE_SLACK for listing in stocks
        )
        if not ended:
            if self.yahoo_left <= 0:
                self.report.deferred += 1
                return
            self.yahoo_left -= 1
            self.attempted += 1
            if self._try_yahoo(row, symbol):
                return
        if stocks and self.tiingo is not None:
            if self.tiingo_blocked:
                self.report.deferred += 1
                return
            if ended:
                self.attempted += 1
            if self._try_tiingo(row, stocks):
                return
            if self.tiingo_blocked:  # spent while trying this row
                self.report.deferred += 1
                return
        self._unpriced(row)

    def _try_yahoo(self, row: _Row, symbol: str) -> bool:
        """True when Yahoo settled the row: priced it, or showed it to be a SPAC or too new."""
        values = probe_yahoo(symbol, row.ipo_date - LOOKBACK, self.today)
        series = _Series.of(values)
        verdict = self._judge(row, series)
        if verdict != "ok" or series is None:
            return self._settle(row, verdict)
        with self.factory() as session:
            instrument = yahoo_common.get_instrument(session, symbol)
            if instrument is not None and self._predates(session, instrument, "yahoo", row):
                return False
            instrument = yahoo_common.get_or_create_instrument(session, symbol)
            upsert_daily_bars(session, instrument.id, values, source="yahoo")
            self._link(session, row, instrument, "yahoo", series.days[0])
        self.report.resolved_yahoo += 1
        return True

    @staticmethod
    def _predates(session: Session, instrument: Instrument, source: str, row: _Row) -> bool:
        """Whether the instrument already holds bars from before this listing.

        Those are another company's — an earlier holder of the ticker — so the listing's bars
        must not be written into the same instrument. Bars that start *later* are a partial
        fetch of the same security, and are completed rather than refused.
        """
        stored = _stored_series(session, instrument.id, source)
        return stored is not None and stored.days[0] < row.ipo_date - FIRST_BAR_EARLY

    def _try_tiingo(self, row: _Row, stocks: list[TiingoListing]) -> bool:
        assert self.tiingo is not None
        for listing in stocks:
            with self.factory() as session:
                stored = find_instrument(session, listing.ticker, listing.exchange)
                if stored is not None and self._predates(session, stored, "tiingo", row):
                    continue
            try:
                self.tiingo.fetch(
                    symbol=listing.ticker,
                    date_from=(row.ipo_date - LOOKBACK).isoformat(),
                    date_to=(listing.end or self.today).isoformat(),
                    exchange=listing.exchange,
                )
            except TiingoBudgetExhausted:
                self.tiingo_blocked = True
                return False
            with self.factory() as session:
                instrument = find_instrument(session, listing.ticker, listing.exchange)
                if instrument is None:
                    continue  # Tiingo had no bars for it
                series = _stored_series(session, instrument.id, "tiingo")
                verdict = self._judge(row, series)
                if verdict == "ok" and series is not None:
                    resolution = f"tiingo:{listing.ticker}"
                    self._link(session, row, instrument, resolution, series.days[0])
                    self.report.resolved_tiingo += 1
                    return True
            if self._settle(row, verdict):
                return True
        return False


def resolve_ipo_prices(
    *,
    since: date,
    max_yahoo: int = 100,
    tiingo: TiingoConnector | None = None,
    tiingo_listings: Mapping[str, list[TiingoListing]] | None = None,
    options: IpoPricingOptions | None = None,
    session_factory: SessionFactory | None = None,
) -> IpoPricingReport:
    """Price the Finnhub calendar's completed IPOs dated ``since`` to today. Idempotent.

    At most ``max_yahoo`` Yahoo probes run; rows past that, or needing Tiingo once its
    budget is spent, are *deferred* (left for the next run) rather than marked unpriced.
    ``tiingo_listings`` (``tiingo.parse_supported_tickers``) also tells a dead listing from a
    live one and an ETF from a stock, so pass it even without ``tiingo``; Tiingo bars are
    fetched only when both are given.

    Per-row provider errors land in ``report.failed`` and the run continues. If every row
    that reached a provider failed, raises ``RuntimeError`` — so a scheduler can tell an
    outage from a quiet run.
    """
    factory = resolve_session_factory(session_factory)
    options = options or IpoPricingOptions()
    now = _utcnow()
    today = options.today or now.date()
    run = _Run(
        factory=factory,
        now=now,
        today=today,
        yahoo_left=max_yahoo,
        tiingo=tiingo if tiingo_listings is not None else None,
        listings=tiingo_listings or {},
    )
    retry_after = now - timedelta(days=options.retry_unpriced_days)
    for row in _pending_rows(factory, since, today, retry_after):
        run.resolve(row)
    run.raise_if_all_failed()
    return run.report
