"""Cold-data archive: Parquet files in object storage (docs/operations/remote-archive.md).

Postgres stays the source of truth for everything the trainer and backtests read (daily
bars, corporate data, features). This package offloads only the bulk that nothing reads in
the hot path — intraday bars and already-scored ``raw`` provider payloads — to an
S3-compatible bucket (Cloudflare R2, Backblaze B2, …) or a local directory, and restores it
on demand. Rows are deleted/nulled locally **only after** the uploaded object has been read
back and verified. Orders and executions are never archived — they are the audit trail.
"""

from data_lake.archive.bars import archive_price_bars, restore_price_bars
from data_lake.archive.catalog import (
    DATASET_SPECS,
    DatasetManifest,
    DatasetSpec,
    list_datasets,
    load_catalog,
    load_manifest,
    rebuild_catalog,
)
from data_lake.archive.raw import archive_raw_payloads, restore_raw_payloads
from data_lake.archive.store import (
    LocalDirStore,
    ObjectStore,
    S3ObjectStore,
    StoredObject,
    store_from_settings,
)

__all__ = [
    "DATASET_SPECS",
    "DatasetManifest",
    "DatasetSpec",
    "LocalDirStore",
    "ObjectStore",
    "S3ObjectStore",
    "StoredObject",
    "archive_price_bars",
    "archive_raw_payloads",
    "list_datasets",
    "load_catalog",
    "load_manifest",
    "rebuild_catalog",
    "restore_price_bars",
    "restore_raw_payloads",
    "store_from_settings",
]
