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

__all__ = ["PricingReport", "covers", "resolve_index_prices"]

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
class _Span:
    id: int
    symbol: str
    start: date
    end: date | None


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


def resolve_index_prices(
    *,
    since: date,
    index_code: str = INDEX_CODE,
    today: date | None = None,
    max_yahoo: int = 100,
    tiingo: TiingoConnector | None = None,
    tiingo_listings: dict[str, list[TiingoListing]] | None = None,
    retry_unpriced_days: int = 30,
    warmup_days: int = 400,
    session_factory: SessionFactory | None = None,
) -> PricingReport:
    """Resolve prices for ``index_code``'s spans live on or after ``since``.

    Bars are fetched from ``warmup_days`` before the span's window, since momentum features
    need about a year of history before a name can be entered. At most ``max_yahoo`` Yahoo
    probes run; spans past that, or needing Tiingo once its budget is spent, are *deferred*
    (left for the next run) rather than marked unpriced.

    Per-span provider errors land in ``report.failed`` and the run continues. If every span
    that reached a provider failed, raises ``RuntimeError`` — so a scheduler can tell an
    outage from a quiet run.
    """
    factory = resolve_session_factory(session_factory)
    today = today or _utcnow().date()
    now = _utcnow()
    retry_after = now - timedelta(days=retry_unpriced_days)

    with factory() as session:
        rows = session.scalars(
            select(IndexMembership)
            .where(
                IndexMembership.index_code == index_code,
                IndexMembership.instrument_id.is_(None),
                or_(IndexMembership.end_date.is_(None), IndexMembership.end_date >= since),
            )
            .order_by(IndexMembership.symbol, IndexMembership.start_date)
        ).all()
        spans = [
            _Span(row.id, row.symbol, row.start_date, row.end_date)
            for row in rows
            if not (
                row.resolution == "unpriced"
                and row.resolved_at is not None
                and _aware(row.resolved_at) > retry_after
            )
        ]

    report = PricingReport()
    yahoo_left = max_yahoo
    use_tiingo = tiingo is not None and tiingo_listings is not None
    tiingo_blocked = False  # budget spent or key refused: defer rather than mark unpriced
    attempted = 0

    for span in spans:
        window_start = max(span.start, since)
        need_to = span.end or today
        fetch_from = window_start - timedelta(days=warmup_days)
        label = f"{span.symbol}@{span.start.isoformat()}"
        yahoo_symbol = _provider_symbol(span.symbol)

        with factory() as session:
            instrument = yahoo_common.get_instrument(session, yahoo_symbol)
            if instrument is not None and _covers(
                _stored_range(session, instrument.id, "yahoo"), window_start, need_to
            ):
                _mark(session, span.id, instrument.id, "yahoo", now)
                report.already += 1
                continue

        if yahoo_left <= 0:
            report.deferred += 1
            continue
        yahoo_left -= 1
        attempted += 1

        try:
            values = _probe_yahoo(yahoo_symbol, fetch_from, need_to)
            if _covers(_values_range(values), window_start, need_to):
                with factory() as session:
                    instrument = yahoo_common.get_or_create_instrument(session, yahoo_symbol)
                    upsert_daily_bars(session, instrument.id, values, source="yahoo")
                    _mark(session, span.id, instrument.id, "yahoo", now)
                report.resolved_yahoo += 1
                continue

            candidates = (
                _usable_listings(span.symbol, tiingo_listings, window_start, need_to)
                if use_tiingo and tiingo_listings is not None
                else []
            )
            if candidates and tiingo_blocked:
                report.deferred += 1
                continue

            resolved = False
            for listing in candidates:
                assert tiingo is not None
                try:
                    tiingo.fetch(
                        symbol=listing.ticker,
                        date_from=fetch_from.isoformat(),
                        date_to=need_to.isoformat(),
                        exchange=listing.exchange,
                    )
                except TiingoBudgetExhausted:
                    tiingo_blocked = True
                    break
                with factory() as session:
                    stored = find_instrument(session, listing.ticker, listing.exchange)
                    if stored is not None and _covers(
                        _stored_range(session, stored.id, "tiingo"), window_start, need_to
                    ):
                        _mark(session, span.id, stored.id, f"tiingo:{listing.ticker}", now)
                        resolved = True
                        break
            if resolved:
                report.resolved_tiingo += 1
                continue
            if tiingo_blocked:
                report.deferred += 1
                continue

            with factory() as session:
                _mark(session, span.id, None, "unpriced", now)
            report.unpriced += 1
        except TiingoProviderError as exc:
            if exc.status in (401, 403):
                tiingo_blocked = True  # the key will not start working mid-run
            report.failed[label] = str(exc)
        except YahooProviderError as exc:
            report.failed[label] = str(exc)

    if attempted and len(report.failed) == attempted:
        sample = "; ".join(f"{k}: {v}" for k, v in list(report.failed.items())[:3])
        raise RuntimeError(f"all {attempted} span(s) sent to a provider failed ({sample})")
    return report
