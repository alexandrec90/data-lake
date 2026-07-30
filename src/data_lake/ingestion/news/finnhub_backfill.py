"""Finnhub company-news history backfill — a self-healing scheduled job.

Finnhub's free tier serves roughly one year of company news, but a request returns at most
~250 articles (observed plateau in our data), so one wide fetch silently truncates. This
module instead walks fixed windows *backwards* from the oldest stored article toward a
rolling floor (today − ``backfill_days``), splitting any window whose response looks capped.

Progress needs no state table: the cursor is ``min(published_at)`` per symbol, recomputed
from the database each run. A finished symbol costs zero requests once the rolling floor
passes its oldest article; an interrupted or budget-stopped run resumes where the stored data
ends. Known limitation: a mid-history gap (e.g. the regular poll down for weeks) is invisible
to the min-date cursor — the regular poll's 7-day overlap covers short outages.
"""

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import NewsArticle
from data_lake.ingestion.base import SessionFactory, resolve_session_factory

logger = logging.getLogger(__name__)

#: A response with at least this many items is assumed truncated by Finnhub's per-request cap
#: (observed ~250 on the free tier) and its window is split in half; a 1-day window that still
#: looks capped is logged and accepted — there is nothing smaller to fetch.
SPLIT_THRESHOLD = 200

#: ``fetch(symbol, date_from, date_to) -> item count`` — injected so the walking logic is
#: testable without network or database.
FetchWindow = Callable[[str, date, date], int]


class _BudgetExhausted(Exception):
    """Internal control flow: the per-run request budget ran out mid-walk."""


def earliest_published_by_symbol(session: Session, source: str = "finnhub") -> dict[str, date]:
    """Oldest stored article date per tagged symbol, in one table scan.

    Computed in Python because ``symbols`` is a JSON list — unnesting it in SQL is
    Postgres-only and the tests run SQLite. Rows are (timestamp, small list), so even a fully
    backfilled table scans in seconds.
    """
    earliest: dict[str, date] = {}
    rows = session.execute(
        select(NewsArticle.published_at, NewsArticle.symbols).where(NewsArticle.source == source)
    )
    for published_at, symbols in rows:
        day = published_at.date()
        for symbol in symbols or []:
            if symbol not in earliest or day < earliest[symbol]:
                earliest[symbol] = day
    return earliest


def _fetch_window_split(fetch: FetchWindow, symbol: str, w_from: date, w_to: date) -> int:
    """Fetch one window, bisecting whenever a response looks capped. Returns items upserted.

    A capped fetch already upserted what it returned; its halves re-cover the full window
    (idempotent upserts), so only un-split fetches count toward the total. Halves share the
    midpoint day on purpose — Finnhub's from/to are inclusive and losing a boundary day is
    worse than re-upserting one.
    """
    total = 0
    stack = [(w_from, w_to)]
    while stack:
        a, b = stack.pop()
        count = fetch(symbol, a, b)
        span_days = (b - a).days
        if count >= SPLIT_THRESHOLD and span_days >= 2:
            mid = a + timedelta(days=span_days // 2)
            stack.append((a, mid))
            stack.append((mid, b))
        else:
            if count >= SPLIT_THRESHOLD:
                logger.warning(
                    "finnhub backfill: %s window %s..%s returned %d items at minimum span — "
                    "accepting a possibly capped day",
                    symbol,
                    a,
                    b,
                    count,
                )
            total += count
    return total


def backfill_symbols(
    symbols: Iterable[str],
    earliest: Mapping[str, date],
    fetch: FetchWindow,
    *,
    today: date,
    backfill_days: int,
    chunk_days: int = 30,
    max_requests: int = 2000,
    request_spacing_seconds: float = 1.1,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Walk each symbol's news history backwards to the rolling floor. Returns items upserted.

    Per symbol, windows of ``chunk_days`` step back from its oldest stored article (or
    ``today`` when nothing is stored) until ``today − backfill_days``; a symbol whose history
    already reaches the floor costs zero requests. At most ``max_requests`` API calls per run —
    hitting the budget defers the remainder to the next run. A failing symbol is logged and
    skipped; calls are spaced for the free-tier rate limit.
    """
    if chunk_days <= 0:
        raise ValueError("chunk_days must be > 0")
    floor = today - timedelta(days=backfill_days)
    total = 0
    requests_used = 0

    def spaced_fetch(fetch_symbol: str, w_from: date, w_to: date) -> int:
        nonlocal requests_used
        if requests_used >= max_requests:
            raise _BudgetExhausted
        if requests_used:
            sleep(request_spacing_seconds)
        requests_used += 1
        return fetch(fetch_symbol, w_from, w_to)

    for symbol in symbols:
        cursor = min(earliest.get(symbol, today), today)
        while cursor > floor:
            window_from = max(floor, cursor - timedelta(days=chunk_days))
            try:
                total += _fetch_window_split(spaced_fetch, symbol, window_from, cursor)
            except _BudgetExhausted:
                logger.warning(
                    "finnhub backfill stopped at the %d-request budget; the rest resumes "
                    "next run (cursor re-derives from stored articles)",
                    max_requests,
                )
                return total
            except Exception:
                logger.exception("finnhub backfill failed for %s; skipping symbol", symbol)
                break
            cursor = window_from
    logger.info(
        "finnhub backfill upserted %d items using %d request(s) down to %s",
        total,
        requests_used,
        floor,
    )
    return total


def run_backfill(
    symbols: Iterable[str],
    *,
    backfill_days: int,
    chunk_days: int = 30,
    max_requests: int = 2000,
    request_spacing_seconds: float = 1.1,
    sleep: Callable[[float], None] = time.sleep,
    session_factory: SessionFactory | None = None,
) -> int:
    """DB/network wrapper: derive per-symbol cursors from Postgres and walk with the real
    connector. Idempotent — safe to run on every ``serve`` start and on a daily interval."""
    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

    session_factory = resolve_session_factory(session_factory)
    with session_factory() as session:
        earliest = earliest_published_by_symbol(session)
    connector = FinnhubNewsConnector(session_factory=session_factory)

    def fetch(symbol: str, date_from: date, date_to: date) -> int:
        return connector.fetch(
            symbol=symbol, date_from=date_from.isoformat(), date_to=date_to.isoformat()
        )

    return backfill_symbols(
        symbols,
        earliest,
        fetch,
        today=datetime.now(UTC).date(),
        backfill_days=backfill_days,
        chunk_days=chunk_days,
        max_requests=max_requests,
        request_spacing_seconds=request_spacing_seconds,
        sleep=sleep,
    )
