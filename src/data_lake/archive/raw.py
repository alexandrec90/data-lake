"""Archive/restore the ``raw`` provider payloads of news articles and social posts.

`maintenance.prune_scored_raw` reclaims disk by destroying scored ``raw`` blobs; this module
is the non-destructive version: the blob is exported to
``raw/<table>/<YYYY-MM>.parquet`` (month of the row's event timestamp), verified, and only
then NULLed locally. The row itself — title, body, sentiment, hashed author — always stays
in Postgres; only the fat provider payload moves. Eligibility mirrors pruning on purpose:
a row must already be sentiment-scored, so an unscored row never loses its payload.

Privacy (Québec Law 25): payloads contain only what ingestion stored — authors are hashed
before they ever reach the DB — but the bucket must still be private; see
docs/operations/remote-archive.md.
"""

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pandas as pd
from sqlalchemy import null, select, update
from sqlalchemy.orm import Session

from data_lake.archive.catalog import record_partition, spec_for
from data_lake.archive.parquet_io import (
    ArchiveResult,
    as_utc,
    chunked,
    key_tuples,
    merge_into_partition,
    parquet_bytes_to_frame,
    verify_partition,
)
from data_lake.archive.store import ObjectStore
from data_lake.db.models import NewsArticle, SocialPost


@dataclass(frozen=True)
class _RawTable:
    model: type[NewsArticle] | type[SocialPost]
    dataset: str  # catalog dataset name, matches a DATASET_SPECS entry
    prefix: str  # partition prefix, e.g. "raw/news_articles/"
    source_column: str  # "source" (news) | "platform" (social)
    event_ts_column: str  # timestamp the partition month comes from


_TABLES = (
    _RawTable(NewsArticle, "news_articles", "raw/news_articles/", "source", "published_at"),
    _RawTable(SocialPost, "social_posts", "raw/social_posts/", "platform", "created_at"),
)
#: Natural key of a payload row inside a partition.
_RAW_KEY = ("source", "external_id")


def _partition_key(table: _RawTable, year: int, month: int) -> str:
    return f"{table.prefix}{year:04d}-{month:02d}.parquet"


def archive_raw_payloads(
    session: Session,
    store: ObjectStore,
    *,
    min_age_days: int = 0,
    now: datetime | None = None,
) -> ArchiveResult:
    """Export scored ``raw`` blobs to Parquet, verify, then NULL them locally.

    ``min_age_days`` is the same ``fetched_at`` grace period pruning uses. Uses ``null()``
    for the SQL NULL (plain None would store JSON 'null' and reclaim nothing).
    """
    if min_age_days < 0:
        raise ValueError("min_age_days must be >= 0")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=min_age_days)

    result = ArchiveResult()
    for table in _TABLES:
        model = table.model
        fetched = cast(
            list[NewsArticle | SocialPost],
            session.execute(
                select(model).where(
                    model.sentiment.is_not(None),
                    model.raw.is_not(None),
                    model.fetched_at <= cutoff,
                )
            )
            .scalars()
            .all(),
        )
        # The SQL filter can't see JSON 'null' (stored for a plain Python None, which is not
        # SQL NULL — see maintenance.prune_scored_raw); drop those payload-less rows here.
        rows = [row for row in fetched if row.raw is not None]
        if not rows:
            continue
        frame = pd.DataFrame.from_records(
            [
                {
                    "_id": row.id,
                    "source": getattr(row, table.source_column),
                    "external_id": row.external_id,
                    "event_ts": as_utc(getattr(row, table.event_ts_column)),
                    "fetched_at": as_utc(row.fetched_at),
                    "raw_json": json.dumps(row.raw),
                }
                for row in rows
            ]
        )
        frame["event_ts"] = pd.to_datetime(frame["event_ts"], utc=True)
        frame["fetched_at"] = pd.to_datetime(frame["fetched_at"], utc=True)

        grouped = frame.groupby([frame["event_ts"].dt.year, frame["event_ts"].dt.month])
        for (year, month), group in sorted(grouped, key=lambda item: item[0]):
            key = _partition_key(table, int(year), int(month))
            upload = group.drop(columns=["_id"])
            merged = merge_into_partition(store, key, upload, _RAW_KEY)
            verify_partition(store, key, key_tuples(upload, _RAW_KEY), _RAW_KEY)
            record_partition(store, spec_for(table.dataset), key, merged, now=now)
            for ids in chunked(group["_id"].tolist()):
                session.execute(update(model).where(model.id.in_(ids)).values(raw=null()))
            result.objects[key] = len(group)
            result.rows_archived += len(group)
            result.rows_removed += len(group)
    return result


def restore_raw_payloads(
    session: Session,
    store: ObjectStore,
    *,
    start: date,
    end: date,
) -> dict[str, int]:
    """Refill NULLed ``raw`` blobs from the archive for rows whose event date is in range.

    Only rows that exist locally and currently have ``raw IS NULL`` are touched — restore
    never overwrites a populated payload and never inserts rows. Returns rows refilled per
    table.
    """
    if start > end:
        raise ValueError("start must be <= end")

    counts: dict[str, int] = {}
    for table in _TABLES:
        model = table.model
        restored = 0
        for obj in store.list_objects(table.prefix):
            ym = obj.key.removeprefix(table.prefix).removesuffix(".parquet")
            try:
                year, month = (int(part) for part in ym.split("-"))
            except ValueError:
                continue
            month_start = date(year, month, 1)
            month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
            if month_start > end or month_end < start:
                continue

            frame = parquet_bytes_to_frame(store.get_bytes(obj.key))
            frame = frame[(frame["event_ts"].dt.date >= start) & (frame["event_ts"].dt.date <= end)]
            payloads = {
                (record["source"], record["external_id"]): record["raw_json"]
                for record in frame.to_dict(orient="records")
            }
            if not payloads:
                continue

            source_col = getattr(model, table.source_column)
            for chunk in chunked(sorted(payloads)):
                sources = {source for source, _ in chunk}
                external_ids = [external_id for _, external_id in chunk]
                candidates = cast(
                    list[NewsArticle | SocialPost],
                    session.execute(
                        select(model).where(
                            source_col.in_(sources),
                            model.external_id.in_(external_ids),
                            model.raw.is_(None),
                        )
                    )
                    .scalars()
                    .all(),
                )
                for row in candidates:
                    payload = payloads.get((getattr(row, table.source_column), row.external_id))
                    if payload is not None:
                        row.raw = json.loads(payload)
                        restored += 1
        session.flush()
        counts[model.__tablename__] = restored
    return counts
