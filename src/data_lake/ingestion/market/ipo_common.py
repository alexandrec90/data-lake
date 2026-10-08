"""The upsert both IPO connectors share (``finnhub_ipo`` and ``sec_edgar``).

One rule matters more than the rest: ``first_seen_at`` is written on insert and **never**
touched again. A consumer's alert dedupes on it, so a refresh that moved it would re-fire every
alert the table has ever raised.

The other is that a refresh never blanks a field. A calendar row that turns ``withdrawn``
arrives with ``price`` and ``exchange`` null, and losing the range it was marketed at is
losing information, not correcting it — so only non-None values overwrite, and ``raw`` is
merged key by key rather than replaced.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import IpoEvent

#: The ``ipo_events.stage`` vocabulary. Providers' own spellings map onto these.
STAGES = frozenset({"filed", "amended", "expected", "priced", "withdrawn"})

#: Columns a refresh may overwrite. Identity (source, external_id) and first_seen_at are not.
UPDATABLE = (
    "company_name",
    "symbol",
    "exchange",
    "cik",
    "stage",
    "form_type",
    "filed_at",
    "expected_date",
    "price_low",
    "price_high",
    "shares",
    "deal_value_usd",
    "url",
    "raw",
)


def upsert_ipo_event(
    session: Session, source: str, external_id: str, fields: dict[str, Any], now: datetime
) -> bool:
    """Insert or refresh one row; True when it was new. ``fields`` keys are column names."""
    if fields.get("stage") not in STAGES:
        raise ValueError(f"stage must be one of {sorted(STAGES)}, got {fields.get('stage')!r}")
    unknown = set(fields) - set(UPDATABLE)
    if unknown:
        raise ValueError(f"not an updatable ipo_events column: {sorted(unknown)}")
    existing = session.scalar(
        select(IpoEvent).where(IpoEvent.source == source, IpoEvent.external_id == external_id)
    )
    if existing is None:
        session.add(
            IpoEvent(
                source=source, external_id=external_id, first_seen_at=now, fetched_at=now, **fields
            )
        )
        session.flush()  # a later row in the same batch must see this one
        return True
    for column, value in fields.items():
        if value is None:
            continue
        if column == "raw":
            # Merged, so a refresh that could not re-derive a key (EDGAR's SIC lookup runs on
            # insert only) does not erase it.
            value = {**(existing.raw or {}), **value}
        setattr(existing, column, value)
    existing.fetched_at = now
    return False
