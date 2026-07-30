"""A self-describing catalog for the object-store archive.

The archive lays Parquet partitions out under stable prefixes (``price_bars/…``,
``raw/news_articles/…``), but a *consumer* — this trader, or a future project sharing the
same bucket — otherwise has to know the naming convention and read code to learn what a
partition contains. The catalog fixes that: alongside the data, under ``_catalog/``, each
dataset gets a small JSON manifest describing its natural key, its timestamp column, its
column schema, and per-partition row counts and time spans. Anything with read access to
the bucket can discover "what's in here" from the manifests alone — no Postgres, no code.

Manifests are metadata, never a source of truth: they are derived from the partitions and
can be rebuilt from them at any time (``rebuild_catalog``). Reading the catalog needs only
``json`` — the pyarrow ``[archive]`` extra is required only to *rebuild* (which re-reads the
Parquet), not to *read* an existing manifest.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from data_lake.archive.parquet_io import as_utc, parquet_bytes_to_frame
from data_lake.archive.store import ObjectStore

#: Where manifests live in the bucket. Kept out of the data prefixes so listing a dataset's
#: partitions never trips over its manifest and vice versa.
CATALOG_PREFIX = "_catalog/"

#: Bumped when the manifest JSON shape changes so consumers can detect an older writer.
CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DatasetSpec:
    """How one logical dataset is stored: where, keyed on what, timestamped by which column.

    ``key_columns`` and ``ts_column`` mirror the archive writers (``bars._BAR_KEY`` /
    ``raw._RAW_KEY``); a test asserts they stay in lockstep so the catalog can never
    advertise a key the data does not actually use.
    """

    name: str
    prefix: str
    ts_column: str
    key_columns: tuple[str, ...]


#: The datasets the archive currently produces. ``price_bars`` spans several ``bar_size=``
#: partitions; the two ``raw`` payload tables are separate datasets.
DATASET_SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        name="price_bars",
        prefix="price_bars/",
        ts_column="ts",
        key_columns=("symbol", "exchange", "currency", "ts", "bar_size", "source", "what_to_show"),
    ),
    DatasetSpec(
        name="news_articles",
        prefix="raw/news_articles/",
        ts_column="event_ts",
        key_columns=("source", "external_id"),
    ),
    DatasetSpec(
        name="social_posts",
        prefix="raw/social_posts/",
        ts_column="event_ts",
        key_columns=("source", "external_id"),
    ),
)

_SPECS_BY_NAME = {spec.name: spec for spec in DATASET_SPECS}


def spec_for(name: str) -> DatasetSpec:
    try:
        return _SPECS_BY_NAME[name]
    except KeyError:
        raise KeyError(f"unknown archive dataset {name!r}") from None


@dataclass(frozen=True)
class PartitionEntry:
    """Catalog facts about one Parquet object: how many rows and what time span it covers."""

    rows: int
    min_ts: datetime
    max_ts: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "min_ts": self.min_ts.isoformat(),
            "max_ts": self.max_ts.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PartitionEntry":
        return cls(
            rows=int(data["rows"]),
            min_ts=_parse_ts(data["min_ts"]),
            max_ts=_parse_ts(data["max_ts"]),
            updated_at=_parse_ts(data["updated_at"]),
        )


@dataclass
class DatasetManifest:
    """The full manifest for one dataset: its contract plus a row per stored partition."""

    dataset: str
    ts_column: str
    key_columns: list[str]
    schema: dict[str, str] = field(default_factory=dict)
    partitions: dict[str, PartitionEntry] = field(default_factory=dict)
    schema_version: int = CATALOG_SCHEMA_VERSION
    updated_at: datetime | None = None

    @property
    def total_rows(self) -> int:
        return sum(entry.rows for entry in self.partitions.values())

    @property
    def min_ts(self) -> datetime | None:
        return min((e.min_ts for e in self.partitions.values()), default=None)

    @property
    def max_ts(self) -> datetime | None:
        return max((e.max_ts for e in self.partitions.values()), default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "schema_version": self.schema_version,
            "ts_column": self.ts_column,
            "key_columns": list(self.key_columns),
            "schema": dict(self.schema),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "partitions": {key: entry.to_dict() for key, entry in sorted(self.partitions.items())},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DatasetManifest":
        return cls(
            dataset=data["dataset"],
            ts_column=data["ts_column"],
            key_columns=list(data["key_columns"]),
            schema=dict(data.get("schema", {})),
            partitions={
                key: PartitionEntry.from_dict(entry)
                for key, entry in data.get("partitions", {}).items()
            },
            schema_version=int(data.get("schema_version", CATALOG_SCHEMA_VERSION)),
            updated_at=_parse_ts(data["updated_at"]) if data.get("updated_at") else None,
        )


def _parse_ts(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(value))


def manifest_key(dataset: str) -> str:
    return f"{CATALOG_PREFIX}{dataset}.json"


def _partition_entry(frame: pd.DataFrame, ts_column: str, now: datetime) -> PartitionEntry:
    ts = pd.to_datetime(frame[ts_column], utc=True)
    return PartitionEntry(
        rows=len(frame),
        min_ts=as_utc(ts.min()),
        max_ts=as_utc(ts.max()),
        updated_at=now,
    )


def _schema_of(frame: pd.DataFrame) -> dict[str, str]:
    return {str(name): str(dtype) for name, dtype in frame.dtypes.items()}


def load_manifest(store: ObjectStore, dataset: str) -> DatasetManifest | None:
    """The stored manifest for ``dataset``, or ``None`` if the dataset was never catalogued."""
    try:
        raw = store.get_bytes(manifest_key(dataset))
    except KeyError:
        return None
    return DatasetManifest.from_dict(json.loads(raw))


def _write_manifest(store: ObjectStore, manifest: DatasetManifest) -> None:
    payload = json.dumps(manifest.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    store.put_bytes(manifest_key(manifest.dataset), payload)


def record_partition(
    store: ObjectStore,
    spec: DatasetSpec,
    partition_key: str,
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> DatasetManifest:
    """Upsert one partition into its dataset manifest from the partition's full merged frame.

    ``frame`` is the whole partition after merge (what the archive uploaded), so the recorded
    row count and time span describe the object as it now stands, not just the newly added
    rows. Read-modify-write on a single manifest object: the archive is single-writer, so no
    locking is needed. Called after the partition has been uploaded and verified.
    """
    now = now or datetime.now(UTC)
    manifest = load_manifest(store, spec.name) or DatasetManifest(
        dataset=spec.name,
        ts_column=spec.ts_column,
        key_columns=list(spec.key_columns),
    )
    manifest.ts_column = spec.ts_column
    manifest.key_columns = list(spec.key_columns)
    manifest.schema = _schema_of(frame)
    manifest.partitions[partition_key] = _partition_entry(frame, spec.ts_column, now)
    manifest.updated_at = now
    _write_manifest(store, manifest)
    return manifest


def list_datasets(store: ObjectStore) -> list[str]:
    """Names of every catalogued dataset (from the manifest objects under ``_catalog/``)."""
    names = []
    for obj in store.list_objects(CATALOG_PREFIX):
        name = obj.key.removeprefix(CATALOG_PREFIX)
        if name.endswith(".json"):
            names.append(name.removesuffix(".json"))
    return sorted(names)


def load_catalog(store: ObjectStore) -> dict[str, DatasetManifest]:
    """Every stored manifest, keyed by dataset name — the whole catalog in one call."""
    catalog = {}
    for name in list_datasets(store):
        manifest = load_manifest(store, name)
        if manifest is not None:
            catalog[name] = manifest
    return catalog


def rebuild_catalog(
    store: ObjectStore, *, now: datetime | None = None
) -> dict[str, DatasetManifest]:
    """Recompute every manifest from scratch by scanning the archived partitions.

    The repair/backfill path: it makes the catalog match reality even for partitions written
    before catalogging existed, or after a manual object change. Reads each partition object,
    so it needs the pyarrow ``[archive]`` extra. Partitions absent from the store are dropped
    from the rebuilt manifest.
    """
    now = now or datetime.now(UTC)
    catalog: dict[str, DatasetManifest] = {}
    for spec in DATASET_SPECS:
        manifest: DatasetManifest | None = None
        for obj in store.list_objects(spec.prefix):
            if not obj.key.endswith(".parquet"):
                continue
            frame = parquet_bytes_to_frame(store.get_bytes(obj.key))
            if manifest is None:
                manifest = DatasetManifest(
                    dataset=spec.name,
                    ts_column=spec.ts_column,
                    key_columns=list(spec.key_columns),
                )
            manifest.schema = _schema_of(frame)
            manifest.partitions[obj.key] = _partition_entry(frame, spec.ts_column, now)
        if manifest is not None:
            manifest.updated_at = now
            _write_manifest(store, manifest)
            catalog[spec.name] = manifest
    return catalog
