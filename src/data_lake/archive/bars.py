"""Archive/restore intraday price bars as monthly Parquet partitions.

Daily bars are the training set and the backtest input — they are small, they stay in
Postgres, and this module **refuses** to archive them. Everything else (minute/hour bars)
is append-only history nothing reads in the hot path: rows older than the cutoff are
uploaded to ``price_bars/bar_size=<slug>/<YYYY-MM>.parquet``, the object is re-read and
verified, and only then are the local rows deleted.

Partitions are self-describing (symbol/exchange/currency, not just instrument_id) so they
can be queried directly with DuckDB/pandas for training and restored into a rebuilt DB.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pandas as pd
from sqlalchemy import delete, select, tuple_
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
from data_lake.db.models import Instrument, PriceBar

#: The one bar size that must never leave Postgres (signals/backtests read it directly).
DAILY_BAR_SIZE = "1 day"

_PREFIX = "price_bars/"
#: Natural key of a bar in the archive (instrument expressed portably, not by local id).
_BAR_KEY = ("symbol", "exchange", "currency", "ts", "bar_size", "source", "what_to_show")
_PARTITION_RE = re.compile(r"^price_bars/bar_size=(?P<slug>[^/]+)/(?P<ym>\d{4}-\d{2})\.parquet$")


@dataclass(frozen=True)
class _Partition:
    key: str
    bar_size_slug: str
    year: int
    month: int


def _partition_key(bar_size: str, year: int, month: int) -> str:
    return f"{_PREFIX}bar_size={bar_size.replace(' ', '_')}/{year:04d}-{month:02d}.parquet"


def _parse_partition(key: str) -> _Partition | None:
    match = _PARTITION_RE.match(key)
    if not match:
        return None
    year, month = (int(part) for part in match.group("ym").split("-"))
    return _Partition(key=key, bar_size_slug=match.group("slug"), year=year, month=month)


def archive_price_bars(
    session: Session,
    store: ObjectStore,
    *,
    older_than_days: int = 365,
    bar_sizes: list[str] | None = None,
    now: datetime | None = None,
) -> ArchiveResult:
    """Upload non-daily bars older than the cutoff, verify the objects, then delete the rows.

    ``bar_sizes=None`` means every bar size except ``"1 day"``; asking for ``"1 day"``
    explicitly is refused. Deletion is per verified partition: if verification of a later
    partition fails, the exception propagates and the caller's transaction rolls back all
    deletes, while the uploads (idempotent merges) simply stay.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    if bar_sizes is not None and DAILY_BAR_SIZE in bar_sizes:
        raise ValueError(
            f"refusing to archive {DAILY_BAR_SIZE!r} bars — they are the training/backtest "
            "input and must stay in Postgres"
        )
    cutoff = (now or datetime.now(UTC)) - timedelta(days=older_than_days)

    query = (
        select(PriceBar, Instrument.symbol, Instrument.exchange, Instrument.currency)
        .join(Instrument, PriceBar.instrument_id == Instrument.id)
        .where(PriceBar.ts < cutoff, PriceBar.bar_size != DAILY_BAR_SIZE)
    )
    if bar_sizes is not None:
        query = query.where(PriceBar.bar_size.in_(bar_sizes))

    records = []
    for bar, symbol, exchange, currency in session.execute(query):
        ts = as_utc(bar.ts)
        records.append(
            {
                "_id": bar.id,
                "symbol": symbol,
                "exchange": exchange,
                "currency": currency,
                "ts": ts,
                "bar_size": bar.bar_size,
                "source": bar.source,
                "what_to_show": bar.what_to_show,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        )

    result = ArchiveResult()
    if not records:
        return result
    frame = pd.DataFrame.from_records(records)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True)

    grouped = frame.groupby([frame["bar_size"], frame["ts"].dt.year, frame["ts"].dt.month])
    for (bar_size, year, month), group in sorted(grouped, key=lambda item: item[0]):
        key = _partition_key(str(bar_size), int(year), int(month))
        upload = group.drop(columns=["_id"])
        merged = merge_into_partition(store, key, upload, _BAR_KEY)
        verify_partition(store, key, key_tuples(upload, _BAR_KEY), _BAR_KEY)
        record_partition(store, spec_for("price_bars"), key, merged, now=now)
        for ids in chunked(group["_id"].tolist()):
            session.execute(delete(PriceBar).where(PriceBar.id.in_(ids)))
        result.objects[key] = len(group)
        result.rows_archived += len(group)
        result.rows_removed += len(group)
    return result


def _month_in_range(partition: _Partition, start: date, end: date) -> bool:
    month_start = date(partition.year, partition.month, 1)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return month_start <= end and month_end >= start


def _instrument_id(session: Session, cache: dict, symbol: str, exchange: str, currency: str) -> int:
    """Resolve (symbol, exchange, currency) to a local instrument, creating it if missing."""
    triple = (symbol, exchange, currency)
    if triple not in cache:
        instrument = session.execute(
            select(Instrument).where(
                Instrument.symbol == symbol,
                Instrument.exchange == exchange,
                Instrument.currency == currency,
            )
        ).scalar_one_or_none()
        if instrument is None:
            instrument = Instrument(symbol=symbol, exchange=exchange, currency=currency)
            session.add(instrument)
            session.flush()
        cache[triple] = instrument.id
    return cache[triple]


def restore_price_bars(
    session: Session,
    store: ObjectStore,
    *,
    start: date,
    end: date,
    bar_sizes: list[str] | None = None,
) -> int:
    """Pull archived bars for [start, end] back into Postgres. Idempotent; returns rows added.

    Instruments are resolved by (symbol, exchange, currency) and created when absent, so a
    restore also works into a rebuilt database.
    """
    if start > end:
        raise ValueError("start must be <= end")
    slugs = {size.replace(" ", "_") for size in bar_sizes} if bar_sizes is not None else None

    partitions = [
        partition
        for obj in store.list_objects(_PREFIX)
        if (partition := _parse_partition(obj.key)) is not None
        and _month_in_range(partition, start, end)
        and (slugs is None or partition.bar_size_slug in slugs)
    ]

    instrument_cache: dict[tuple[str, str, str], int] = {}
    restored = 0
    for partition in partitions:
        frame = parquet_bytes_to_frame(store.get_bytes(partition.key))
        frame = frame[(frame["ts"].dt.date >= start) & (frame["ts"].dt.date <= end)]
        if frame.empty:
            continue

        rows = []
        for record in frame.to_dict(orient="records"):
            rows.append(
                {
                    **record,
                    "ts": as_utc(record["ts"]),
                    "volume": None if pd.isna(record["volume"]) else float(record["volume"]),
                    "instrument_id": _instrument_id(
                        session,
                        instrument_cache,
                        record["symbol"],
                        record["exchange"],
                        record["currency"],
                    ),
                }
            )

        existing: set[tuple] = set()
        keys = [
            (r["instrument_id"], r["ts"], r["bar_size"], r["source"], r["what_to_show"])
            for r in rows
        ]
        bar_key_cols = (
            PriceBar.instrument_id,
            PriceBar.ts,
            PriceBar.bar_size,
            PriceBar.source,
            PriceBar.what_to_show,
        )
        for chunk in chunked(keys):
            found = session.execute(
                select(*bar_key_cols).where(tuple_(*bar_key_cols).in_(chunk))
            ).all()
            existing |= {(iid, as_utc(ts), bs, src, wts) for iid, ts, bs, src, wts in found}

        for row, key in zip(rows, keys, strict=True):
            if key in existing:
                continue
            session.add(
                PriceBar(
                    instrument_id=row["instrument_id"],
                    ts=row["ts"],
                    bar_size=row["bar_size"],
                    source=row["source"],
                    what_to_show=row["what_to_show"],
                    open=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=row["volume"],
                )
            )
            restored += 1
        session.flush()
    return restored
