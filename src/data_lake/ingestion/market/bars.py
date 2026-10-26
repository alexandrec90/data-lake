"""Daily-bar reads and writes shared by the price connectors.

A per-bar ``SELECT`` is fine for a daily incremental poll of a handful of symbols, and far too
slow for a backfill of ~850 symbols x ~4,000 bars. :func:`upsert_daily_bars` instead preloads
every existing row in the incoming range with one query and then updates or inserts in memory.

:func:`latest_bar_ts` is the "where did this series stop" lookup every incremental poll starts
from. On TimescaleDB, ``price_bars`` is a hypertable of ~790 monthly chunks (1961 onwards), and
a query with no constant ``ts`` bound plans over every one of them: the backend opens each chunk,
its compressed twin and their indexes, and keeps that catalog cache until it disconnects. One
unbounded ``max(ts)`` grew a backend from 1.4 MB to 63 MB and took 14.8 s; the same lookup bounded
to the last 35 days took 2 MB and 0.03 s. Four such backends at once got the 768m db container
OOM-killed six times on 2026-10-10/11.

:func:`upsert_daily_bars` refuses a bar whose open or close lies outside its own low/high (see
:func:`bar_is_consistent`). Every one of those seen so far was a bar Yahoo had not finished yet:
fetched mid-session (CPAY 2026-10-08, 3,742 shares against a final 670,000) or minutes after the
close, before Yahoo settled the day's low. The same days re-fetched later all came back
consistent. Skipping such a bar leaves a gap for the next poll's re-fetch of its newest day to
fill, which beats storing a row a consumer's price validation fails on.
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, DateTime, Select, literal, select
from sqlalchemy.orm import Session

from data_lake.db.models import PriceBar

__all__ = [
    "LATEST_BAR_LOOKBACKS",
    "bar_is_consistent",
    "bar_ts_since",
    "latest_bar_ts",
    "upsert_daily_bars",
]

logger = logging.getLogger(__name__)

#: Float slack for :func:`bar_is_consistent`, relative to the price. ``auto_adjust`` rescales
#: all four prices by one factor, which costs ~1e-15, so this only absorbs rounding noise. It is
#: tighter than the 1e-7 a consumer's validation allows, so every bar it admits passes there too.
CONSISTENCY_TOLERANCE = 1e-9

#: The windows :func:`latest_bar_ts` tries, nearest first, before it scans the whole history.
#: A series polled on schedule always has a bar in the first; the second catches one paused for
#: months. Only a series idle for over a year, or never stored, pays the unbounded scan.
LATEST_BAR_LOOKBACKS = (timedelta(days=45), timedelta(days=400))


def bar_ts_since(cutoff: datetime) -> ColumnElement[bool]:
    """``PriceBar.ts >= cutoff``, with ``cutoff`` written into the SQL as a literal.

    A literal, not a bound parameter, because chunk exclusion at plan time needs a constant: a
    driver that prepares a repeated statement (psycopg does from its fifth run) can get a
    generic plan, which plans over every chunk whatever the parameter's value.
    """
    return PriceBar.ts >= literal(cutoff, DateTime(timezone=True), literal_execute=True)


def latest_bar_ts(
    session: Session, query: Select[datetime], *, now: datetime | None = None
) -> datetime | None:
    """Run ``query`` -- a ``select(func.max(PriceBar.ts))`` with its filters -- cheaply.

    Tries each of :data:`LATEST_BAR_LOOKBACKS` in turn, bounded by :func:`bar_ts_since`, and
    falls back to ``query`` as given only when none of them holds a bar. The answer is the same
    as running ``query`` alone; only the cost differs.
    """
    now = now or datetime.now(UTC)
    for lookback in LATEST_BAR_LOOKBACKS:
        found = session.scalar(query.where(bar_ts_since(now - lookback)))
        if found is not None:
            return found
    return session.scalar(query)


def _ts_key(ts: datetime) -> datetime:
    """Naive-UTC key, so a bar read back from SQLite (naive) matches an incoming aware one."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def bar_is_consistent(row: dict[str, Any]) -> bool:
    """Whether ``row``'s open and close both lie within its ``[low, high]`` (and so low <= high).

    A NaN anywhere fails every comparison, so it reads as inconsistent too.
    """
    low, high = row["low"], row["high"]
    slack = CONSISTENCY_TOLERANCE * max(abs(low), abs(high))
    return all(low - slack <= row[field] <= high + slack for field in ("open", "close"))


def upsert_daily_bars(
    session: Session,
    instrument_id: int,
    values: Iterable[dict[str, Any]],
    *,
    source: str,
    what_to_show: str = "ADJUSTED_LAST",
    bar_size: str = "1 day",
) -> int:
    """Upsert ``{ts, open, high, low, close, volume}`` dicts as bars of one instrument/source.

    Returns the number of bars written (inserted or updated). A ``ts`` repeated within
    ``values`` updates the same row rather than violating the unique constraint.

    A bar failing :func:`bar_is_consistent` is logged and skipped, not clipped: a row already
    stored for its ``ts`` is left as it was, and it does not count as written.
    """
    rows = []
    for row in values:
        if bar_is_consistent(row):
            rows.append(row)
        else:
            logger.warning(
                "skipped inconsistent %s bar: instrument %s %s O %s H %s L %s C %s V %s",
                source,
                instrument_id,
                row["ts"].date(),
                row["open"],
                row["high"],
                row["low"],
                row["close"],
                row["volume"],
            )
    if not rows:
        return 0

    stamps = [row["ts"] for row in rows]
    existing = {
        _ts_key(bar.ts): bar
        for bar in session.scalars(
            select(PriceBar).where(
                PriceBar.instrument_id == instrument_id,
                PriceBar.source == source,
                PriceBar.what_to_show == what_to_show,
                PriceBar.bar_size == bar_size,
                PriceBar.ts >= min(stamps),
                PriceBar.ts <= max(stamps),
            )
        )
    }

    for row in rows:
        key = _ts_key(row["ts"])
        bar = existing.get(key)
        if bar is None:
            bar = PriceBar(
                instrument_id=instrument_id,
                ts=row["ts"],
                bar_size=bar_size,
                source=source,
                what_to_show=what_to_show,
            )
            session.add(bar)
            existing[key] = bar
        bar.open = row["open"]
        bar.high = row["high"]
        bar.low = row["low"]
        bar.close = row["close"]
        bar.volume = row["volume"]
    return len(rows)
