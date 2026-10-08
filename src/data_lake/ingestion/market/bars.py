"""Bulk daily-bar upsert shared by the price connectors.

A per-bar ``SELECT`` is fine for a daily incremental poll of a handful of symbols, and far too
slow for a backfill of ~850 symbols x ~4,000 bars. :func:`upsert_daily_bars` instead preloads
every existing row in the incoming range with one query and then updates or inserts in memory.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import PriceBar

__all__ = ["upsert_daily_bars"]


def _ts_key(ts: datetime) -> datetime:
    """Naive-UTC key, so a bar read back from SQLite (naive) matches an incoming aware one."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


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
    """
    rows = list(values)
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
