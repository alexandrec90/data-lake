"""Find prices for every index membership span, including members that later died.

For each unresolved span in ``index_memberships`` this tries, in order:

a. bars already stored — a ``(symbol, SMART, USD)`` instrument whose Yahoo bars cover the
   span: resolved with no network call;
b. a Yahoo probe — stored only if the download covers the span, so a dead or reused ticker
   never leaves an empty or wrong instrument behind;
c. Tiingo, for tickers Yahoo has dropped — the span's ticker and its bankruptcy ``Q``
   ticker, each usable only when Tiingo lists it *exactly once*: a reused ticker is listed
   several times, and there is no telling which company the API would serve;
d. otherwise ``unpriced``, retried after ``retry_unpriced_days``.

**No rename is ever guessed** (``ANTM`` -> ``ELV``): a wrong guess prices one company with
another's history, which is worse than a span left visibly unpriced for the consumer's
coverage report.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from data_lake.db.models import IndexMembership, PriceBar
from data_lake.ingestion.base import SessionFactory, resolve_session_factory
from data_lake.ingestion.market import yahoo, yahoo_common
from data_lake.ingestion.market.bars import upsert_daily_bars
from data_lake.ingestion.market.index_membership import INDEX_CODE
from data_lake.ingestion.market.tiingo import (
    TiingoBudgetExhausted,
    TiingoConnector,
    TiingoListing,
    TiingoProviderError,
    find_instrument,
)
from data_lake.ingestion.market.yahoo_common import YahooProviderError

__all__ = ["PricingOptions", "PricingReport", "covers", "resolve_index_prices"]

#: Slack on each end of a span: a listing that starts within a month of the window, or bars
#: that stop within ten days of its end, still count as covering it.
START_SLACK = timedelta(days=31)
END_SLACK = timedelta(days=10)


@dataclass
class PricingReport:
    already: int = 0
    resolved_yahoo: int = 0
    resolved_tiingo: int = 0
    unpriced: int = 0
    deferred: int = 0
    #: ``"SYMBOL@start"`` -> error, for spans whose provider call raised.
    failed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PricingOptions:
    """The knobs a scheduled run leaves at their defaults.

    ``today`` defaults to the current UTC date. Unpriced spans are retried after
    ``retry_unpriced_days``; bars are fetched from ``warmup_days`` before a span's window.
    """

    index_code: str = INDEX_CODE
    today: date | None = None
    retry_unpriced_days: int = 30
    warmup_days: int = 400


@dataclass(frozen=True)
class _Span:
    id: int
    symbol: str
    start: date
    end: date | None


@dataclass(frozen=True)
class _Window:
    """One span's pricing window: ``start..need_to``, fetched from ``fetch_from``."""

    span: _Span
    start: date
    need_to: date
    fetch_from: date

    @property
    def label(self) -> str:
        return f"{self.span.symbol}@{self.span.start.isoformat()}"

    @property
    def provider_symbol(self) -> str:
        return _provider_symbol(self.span.symbol)

    def is_covered_by(self, span_range: tuple[date, date] | None) -> bool:
        return _covers(span_range, self.start, self.need_to)


def covers(first_bar: date, last_bar: date, window_start: date, need_to: date) -> bool:
    """Whether a series from ``first_bar`` to ``last_bar`` prices ``window_start..need_to``."""
    return first_bar <= window_start + START_SLACK and last_bar >= need_to - END_SLACK


def _covers(span_range: tuple[date, date] | None, window_start: date, need_to: date) -> bool:
    return span_range is not None and covers(*span_range, window_start, need_to)


def _provider_symbol(symbol: str) -> str:
    """Yahoo and Tiingo spell class shares with a dash (``BRK-B``), the index file a dot."""
    return symbol.replace(".", "-")


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def _stored_range(session: Session, instrument_id: int, source: str) -> tuple[date, date] | None:
    first, last = session.execute(
        select(func.min(PriceBar.ts), func.max(PriceBar.ts)).where(
            PriceBar.instrument_id == instrument_id,
            PriceBar.source == source,
            PriceBar.what_to_show == "ADJUSTED_LAST",
            PriceBar.bar_size == "1 day",
        )
    ).one()
    return None if first is None else (first.date(), last.date())


def _values_range(values: list[dict[str, Any]]) -> tuple[date, date] | None:
    if not values:
        return None
    days = [value["ts"].date() for value in values]
    return min(days), max(days)


def _ticker_missing(error: BaseException | None) -> bool:
    """Yahoo's "possibly delisted" — an answer (no data), not an outage."""
    from yfinance.exceptions import YFTickerMissingError

    return isinstance(error, YFTickerMissingError)


def _probe_yahoo(symbol: str, start: date, need_to: date) -> list[dict[str, Any]]:
    yahoo_common.throttle()
    try:
        frame = yahoo._download_within_timeout(
            symbol,
            start,
            need_to + timedelta(days=1),  # yfinance's `end` is exclusive
            auto_adjust=True,
            timeout=yahoo_common.DOWNLOAD_TIMEOUT_SECONDS,
        )
    except YahooProviderError as exc:
        if _ticker_missing(exc.__cause__):
            return []
        raise
    return yahoo._bar_values(frame)


def _usable_listings(
    symbol: str, listings: dict[str, list[TiingoListing]], window_start: date, need_to: date
) -> list[TiingoListing]:
    """Tiingo tickers that can price the span: the symbol, then its ``Q`` bankruptcy ticker."""
    usable = []
    base = _provider_symbol(symbol)
    for candidate in (base, base + "Q"):
        rows = listings.get(candidate, [])
        if len(rows) != 1 or rows[0].asset_type != "Stock":
            continue  # absent, or a reused ticker we cannot disambiguate
        row = rows[0]
        if covers(row.start, row.end or need_to, window_start, need_to):
            usable.append(row)
    return usable


def _mark(
    session: Session,
    span_id: int,
    instrument_id: int | None,
    resolution: str,
    now: datetime,
) -> None:
    row = session.get(IndexMembership, span_id)
    assert row is not None
    row.instrument_id = instrument_id
    row.resolution = resolution
    row.resolved_at = now


def _pending_spans(
    factory: SessionFactory, since: date, options: PricingOptions, now: datetime
) -> list[_Span]:
    """Unresolved spans live on or after ``since``, minus those marked unpriced too recently."""
    retry_after = now - timedelta(days=options.retry_unpriced_days)
    with factory() as session:
        rows = session.scalars(
            select(IndexMembership)
            .where(
                IndexMembership.index_code == options.index_code,
                IndexMembership.instrument_id.is_(None),
                or_(IndexMembership.end_date.is_(None), IndexMembership.end_date >= since),
            )
            .order_by(IndexMembership.symbol, IndexMembership.start_date)
        ).all()
        return [
            _Span(row.id, row.symbol, row.start_date, row.end_date)
            for row in rows
            if not (
                row.resolution == "unpriced"
                and row.resolved_at is not None
                and _aware(row.resolved_at) > retry_after
            )
        ]


def _window(span: _Span, since: date, today: date, warmup_days: int) -> _Window:
    start = max(span.start, since)
    return _Window(span, start, span.end or today, start - timedelta(days=warmup_days))


@dataclass
class _Run:
    """One run's state: what is left of the Yahoo budget, and whether Tiingo is blocked."""

    factory: SessionFactory
    now: datetime
    yahoo_left: int
    tiingo: TiingoConnector | None
    listings: dict[str, list[TiingoListing]]
    #: Budget spent or key refused: defer rather than mark unpriced.
    tiingo_blocked: bool = False
    attempted: int = 0
    report: PricingReport = field(default_factory=PricingReport)

    def resolve(self, window: _Window) -> None:
        if self._already_stored(window):
            self.report.already += 1
            return
        if self.yahoo_left <= 0:
            self.report.deferred += 1
            return
        self.yahoo_left -= 1
        self.attempted += 1
        try:
            self._resolve_with_providers(window)
        except TiingoProviderError as exc:
            if exc.status in (401, 403):
                self.tiingo_blocked = True  # the key will not start working mid-run
            self.report.failed[window.label] = str(exc)
        except YahooProviderError as exc:
            self.report.failed[window.label] = str(exc)

    def raise_if_all_failed(self) -> None:
        failed = self.report.failed
        if self.attempted and len(failed) == self.attempted:
            sample = "; ".join(f"{k}: {v}" for k, v in list(failed.items())[:3])
            raise RuntimeError(f"all {self.attempted} span(s) sent to a provider failed ({sample})")

    def _already_stored(self, window: _Window) -> bool:
        with self.factory() as session:
            instrument = yahoo_common.get_instrument(session, window.provider_symbol)
            if instrument is None or not window.is_covered_by(
                _stored_range(session, instrument.id, "yahoo")
            ):
                return False
            _mark(session, window.span.id, instrument.id, "yahoo", self.now)
            return True

    def _resolve_with_providers(self, window: _Window) -> None:
        if self._try_yahoo(window):
            self.report.resolved_yahoo += 1
            return
        if self.tiingo_blocked:
            self.report.deferred += 1
            return
        if self._try_tiingo(window):
            self.report.resolved_tiingo += 1
            return
        if self.tiingo_blocked:  # spent while trying this span
            self.report.deferred += 1
            return
        with self.factory() as session:
            _mark(session, window.span.id, None, "unpriced", self.now)
        self.report.unpriced += 1

    def _try_yahoo(self, window: _Window) -> bool:
        values = _probe_yahoo(window.provider_symbol, window.fetch_from, window.need_to)
        if not window.is_covered_by(_values_range(values)):
            return False
        with self.factory() as session:
            instrument = yahoo_common.get_or_create_instrument(session, window.provider_symbol)
            upsert_daily_bars(session, instrument.id, values, source="yahoo")
            _mark(session, window.span.id, instrument.id, "yahoo", self.now)
        return True

    def _try_tiingo(self, window: _Window) -> bool:
        if self.tiingo is None:
            return False
        usable = _usable_listings(window.span.symbol, self.listings, window.start, window.need_to)
        for listing in usable:
            try:
                self.tiingo.fetch(
                    symbol=listing.ticker,
                    date_from=window.fetch_from.isoformat(),
                    date_to=window.need_to.isoformat(),
                    exchange=listing.exchange,
                )
            except TiingoBudgetExhausted:
                self.tiingo_blocked = True
                return False
            if self._mark_tiingo(window, listing):
                return True
        return False

    def _mark_tiingo(self, window: _Window, listing: TiingoListing) -> bool:
        with self.factory() as session:
            stored = find_instrument(session, listing.ticker, listing.exchange)
            if stored is None or not window.is_covered_by(
                _stored_range(session, stored.id, "tiingo")
            ):
                return False
            _mark(session, window.span.id, stored.id, f"tiingo:{listing.ticker}", self.now)
            return True


def resolve_index_prices(
    *,
    since: date,
    max_yahoo: int = 100,
    tiingo: TiingoConnector | None = None,
    tiingo_listings: dict[str, list[TiingoListing]] | None = None,
    options: PricingOptions | None = None,
    session_factory: SessionFactory | None = None,
) -> PricingReport:
    """Resolve prices for the index's spans live on or after ``since``.

    Bars are fetched from ``options.warmup_days`` before the span's window, since momentum
    features need about a year of history before a name can be entered. At most
    ``max_yahoo`` Yahoo probes run; spans past that, or needing Tiingo once its budget is
    spent, are *deferred* (left for the next run) rather than marked unpriced. Tiingo is
    tried only when both ``tiingo`` and ``tiingo_listings`` are given.

    Per-span provider errors land in ``report.failed`` and the run continues. If every span
    that reached a provider failed, raises ``RuntimeError`` — so a scheduler can tell an
    outage from a quiet run.
    """
    factory = resolve_session_factory(session_factory)
    options = options or PricingOptions()
    today = options.today or _utcnow().date()
    now = _utcnow()
    run = _Run(
        factory=factory,
        now=now,
        yahoo_left=max_yahoo,
        tiingo=tiingo if tiingo_listings is not None else None,
        listings=tiingo_listings or {},
    )
    for span in _pending_spans(factory, since, options, now):
        run.resolve(_window(span, since, today, options.warmup_days))
    run.raise_if_all_failed()
    return run.report
