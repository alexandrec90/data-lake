"""Shared Parquet plumbing for the archive: serialization, partition merge, verification.

pyarrow lives behind the ``[archive]`` extra — modules import this lazily and fail with an
install hint only when archiving is actually attempted. Partitions are merged, never
overwritten blindly: an existing object is read, new rows are appended, duplicates on the
partition's natural key collapse to the already-archived row. Nothing is ever removed from
a partition here.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO

import pandas as pd

from data_lake.archive.store import ObjectStore

#: Bound on SQL ``IN (...)`` parameter lists (SQLite's historical limit is 999).
SQL_IN_CHUNK = 500


@dataclass
class ArchiveResult:
    """What one archive run did: rows newly written per object, and rows removed locally."""

    objects: dict[str, int] = field(default_factory=dict)
    rows_archived: int = 0
    rows_removed: int = 0


def _require_pyarrow() -> None:
    try:
        import pyarrow  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "archiving needs the archive extra — install with: uv sync --extra archive"
        ) from exc


def frame_to_parquet_bytes(frame: pd.DataFrame) -> bytes:
    _require_pyarrow()
    buffer = BytesIO()
    frame.to_parquet(buffer, engine="pyarrow", index=False, compression="zstd")
    return buffer.getvalue()


def parquet_bytes_to_frame(data: bytes) -> pd.DataFrame:
    _require_pyarrow()
    return pd.read_parquet(BytesIO(data), engine="pyarrow")


def as_utc(value: datetime | pd.Timestamp) -> datetime:
    """Normalize to a tz-aware UTC ``datetime`` (SQLite returns naive UTC timestamps)."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def key_tuples(frame: pd.DataFrame, columns: Sequence[str]) -> set[tuple]:
    """The frame's natural-key tuples, timestamps normalized so DB and Parquet rows match."""
    keys: set[tuple] = set()
    for row in frame[list(columns)].itertuples(index=False):
        keys.add(tuple(as_utc(v) if isinstance(v, (datetime, pd.Timestamp)) else v for v in row))
    return keys


def merge_into_partition(
    store: ObjectStore, key: str, new_rows: pd.DataFrame, dedupe_on: Sequence[str]
) -> pd.DataFrame:
    """Append ``new_rows`` to the partition object, deduped on its natural key, and upload.

    Existing archived rows win on collision (archived data is immutable). Returns the merged
    frame that was uploaded.
    """
    try:
        existing = parquet_bytes_to_frame(store.get_bytes(key))
    except KeyError:
        existing = None
    merged = new_rows if existing is None else pd.concat([existing, new_rows], ignore_index=True)
    merged = merged.drop_duplicates(subset=list(dedupe_on), keep="first")
    merged = merged.sort_values(list(dedupe_on)).reset_index(drop=True)
    store.put_bytes(key, frame_to_parquet_bytes(merged))
    return merged


def verify_partition(store: ObjectStore, key: str, expected: set[tuple], columns: Sequence[str]):
    """Re-read the uploaded object and prove every expected natural key is really in it.

    This is the gate between "uploaded" and "safe to delete locally": deletion callers must
    only remove rows whose keys passed this check.
    """
    try:
        data = store.get_bytes(key)
    except KeyError:
        raise RuntimeError(
            f"archive verification failed: {key} is missing from the store"
        ) from None
    try:
        stored = key_tuples(parquet_bytes_to_frame(data), columns)
    except Exception as exc:  # any parse failure means "not verified"
        raise RuntimeError(f"archive verification failed: cannot re-read {key}: {exc}") from exc
    missing = expected - stored
    if missing:
        raise RuntimeError(
            f"archive verification failed: {key} lacks {len(missing)} of "
            f"{len(expected)} expected rows — local rows were NOT touched"
        )


def chunked(values: Sequence, size: int = SQL_IN_CHUNK) -> Iterator[Sequence]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
