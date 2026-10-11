"""The ``earnings_events`` upsert both earnings sources share (``yahoo_fundamentals`` and
``finnhub_earnings``).

Three rules, the same as ``ipo_common``'s and for the same reasons:

- ``first_seen_at`` is written on insert and never touched again. It is the point-in-time
  "when did we know this report was scheduled" that ``report_ts`` is not.
- A refresh never blanks a field. An estimate a provider stops quoting after the report is
  still the estimate the market had, so only non-None values overwrite.
- An *upcoming* report the provider no longer lists is deleted. Both providers re-date
  reports as companies confirm them, and the old timestamp would otherwise stay behind as a
  ghost report that never happens. Past reports are never deleted.
"""

from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import EarningsEvent

#: Columns a refresh may overwrite. Identity (instrument, report_ts, source) and
#: first_seen_at are not.
UPDATABLE = (
    "eps_estimate",
    "eps_actual",
    "surprise_pct",
    "revenue_estimate",
    "revenue_actual",
    "hour",
)


def as_utc(value: datetime) -> datetime:
    """An aware UTC datetime. SQLite hands back naive values, which were stored as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def upsert_earnings_event(
    session: Session,
    instrument_id: int,
    report_ts: datetime,
    source: str,
    fields: dict[str, Any],
    now: datetime,
) -> bool:
    """Insert or refresh one row; True when it was new. ``fields`` keys are column names."""
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise ValueError(f"not an updatable earnings_events column: {sorted(unknown)}")
    existing = session.scalar(
        select(EarningsEvent).where(
            EarningsEvent.instrument_id == instrument_id,
            EarningsEvent.report_ts == report_ts,
            EarningsEvent.source == source,
        )
    )
    if existing is None:
        session.add(
            EarningsEvent(
                instrument_id=instrument_id,
                report_ts=report_ts,
                source=source,
                first_seen_at=now,
                fetched_at=now,
                **fields,
            )
        )
        session.flush()  # a later row in the same batch must see this one
        return True
    for column, value in fields.items():
        if value is not None:
            setattr(existing, column, value)
    existing.fetched_at = now
    return False


def delete_vanished_future_earnings(
    session: Session,
    *,
    source: str,
    keep: Collection[tuple[int, datetime]],
    after: datetime,
    instrument_ids: Collection[int] | None = None,
    until: datetime | None = None,
) -> int:
    """Delete ``source`` rows dated after ``after`` (never earlier than now, in practice) and
    before ``until`` that are not in ``keep``, a set of ``(instrument_id, report_ts)`` in UTC.
    Returns rows deleted.

    ``instrument_ids`` limits the sweep to the instruments the caller actually re-read;
    ``until`` to the window it re-read. Call it only after a non-empty provider response —
    an empty one is as likely an outage as a cleared calendar.
    """
    query = select(EarningsEvent).where(
        EarningsEvent.source == source, EarningsEvent.report_ts > after
    )
    if instrument_ids is not None:
        query = query.where(EarningsEvent.instrument_id.in_(list(instrument_ids)))
    if until is not None:
        query = query.where(EarningsEvent.report_ts < until)
    wanted = {(instrument_id, as_utc(ts)) for instrument_id, ts in keep}
    deleted = 0
    for row in session.scalars(query).all():
        if (row.instrument_id, as_utc(row.report_ts)) not in wanted:
            session.delete(row)
            deleted += 1
    return deleted
