"""Social posts (Reddit, X) from the social-scraper project's lake export — no API key.

social-scraper drives a real browser, keylessly, and publishes what it collects to the
pooled archive as Hive-style Parquet with a ``_catalog/`` manifest. This connector reads
its ``social_scraper_posts`` dataset through this package's own archive layer (the
configured :class:`~data_lake.archive.store.ObjectStore`, local or S3) and upserts it into
``social_posts``. It replaced the PRAW connector, which needed a Reddit API key the owner
cannot obtain.

The contract is social-scraper's (its README, "Data contract"): the dataset uses this
table's column names, keyed on ``(platform, external_id)``, partitioned as
``social_scraper/posts/platform=<p>/dt=<created date>/part.parquet``. Reddit ids and
``author_hash`` match what the PRAW connector stored, so rows from both sources dedupe.
Authors arrive already hashed; nothing here sees a username.

**Incremental, at two levels.** A partition is a day of one platform's posts, rewritten by
merge whenever a late comment or refreshed score lands in it, and the manifest stamps each
partition with the export run that last wrote it (``updated_at``). The watermark is this
table's own ``max(fetched_at)`` per platform — the pattern the bar connectors use with
``max(ts)``. Only partitions written at or after ``watermark - lookback`` are read: a row's
``fetched_at`` (social-scraper's ``last_seen_at``) is stamped before the export that writes
it, on the same clock, so an older partition holds nothing newer than what is already
loaded. ``lookback`` absorbs the exceptions — an export still running when the previous
fetch read the catalog, or a scrape stamping rows while an export is under way. Within a partition, a row is written only when it is new or
carries a later ``fetched_at`` than the stored copy, so re-reading a partition is idempotent
and the returned count is rows actually inserted or refreshed.

**A missing dataset raises.** ``fetch`` reports success as a count, and a consumer's job
health reads a returned 0 as "ran fine, nothing new". An archive with no
``social_scraper_posts`` manifest is not that: it means social-scraper has never exported to
*this* store, or the archive settings point at a different tree or bucket — either way the
pipeline delivers nothing, and only an error makes that visible. Once the dataset exists its
manifest is never removed, so the error cannot flap.

**Memory is one partition deep.** A run can select many partitions — a backlog after an
outage, or a bulk import on social-scraper's side that rewrites many days at once — so each
is flushed and released before the next is read. Holding every post until the end grew a
consumer's scheduler to three quarters of its Docker VM's memory.
"""

import logging
import math
import re
from collections.abc import Collection, Iterator
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.archive.catalog import DatasetManifest, PartitionEntry, load_manifest
from data_lake.archive.parquet_io import as_utc, chunked, parquet_bytes_to_frame
from data_lake.archive.store import ObjectStore, store_from_settings
from data_lake.db.models import SocialPost
from data_lake.ingestion.base import Connector, SessionFactory
from data_lake.settings import LakeSettings

logger = logging.getLogger(__name__)

#: social-scraper's catalog name for its posts dataset.
DATASET = "social_scraper_posts"

#: How far before the watermark a partition's export stamp may fall and still be re-read.
DEFAULT_LOOKBACK = timedelta(hours=1)

#: Columns social-scraper exports beyond this table's own, kept in ``raw`` because nothing
#: else stores them. A fixed, small set — never the whole row (the ``url`` and author reach
#: columns stay in the lake).
RAW_FIELDS = (
    "kind",
    "parent_id",
    "lang",
    "flair",
    "upvote_ratio",
    "likes",
    "reposts",
    "quotes",
    "views",
    "bookmarks",
    "stance",
)

_PLATFORM_IN_KEY = re.compile(r"(?:^|/)platform=([^/]+)/")

_Key = tuple[str, str]


class SocialScraperDatasetMissing(RuntimeError):
    """The archive holds no ``social_scraper_posts`` manifest: nothing to ingest from."""


def partition_platform(key: str) -> str | None:
    """The ``platform=`` segment of a partition key, or ``None`` if it has none."""
    match = _PLATFORM_IN_KEY.search(key)
    return match.group(1) if match else None


def plain(value: Any) -> Any:
    """A Parquet cell as pandas hands it back, made plain Python for SQLAlchemy.

    Nulls arrive as ``None``, ``NaN`` (an integer column with a gap reads as float) or
    ``NaT``; lists arrive as numpy arrays; numbers as numpy scalars; timestamps as
    ``pd.Timestamp``.
    """
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return as_utc(value)
    if isinstance(value, (np.ndarray, list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


def _raw(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = {name: row[name] for name in RAW_FIELDS if row.get(name) is not None}
    return raw or None


def select_partitions(
    manifest: DatasetManifest,
    watermarks: dict[str, datetime],
    platforms: Collection[str] | None,
    lookback: timedelta,
) -> list[tuple[str, PartitionEntry]]:
    """The partitions worth reading, oldest export first.

    A partition whose key names no platform is always read: there is no watermark to hold
    it against, and skipping it would be silent loss.
    """
    chosen = []
    for key, entry in manifest.partitions.items():
        platform = partition_platform(key)
        if platforms is not None and platform is not None and platform not in platforms:
            continue
        watermark = watermarks.get(platform) if platform is not None else None
        if watermark is None or entry.updated_at >= watermark - lookback:
            chosen.append((key, entry))
    return sorted(chosen, key=lambda item: (item[1].updated_at, item[0]))


def _watermarks(session: Session) -> dict[str, datetime]:
    """``max(fetched_at)`` per platform already in ``social_posts``."""
    rows = session.execute(
        select(SocialPost.platform, func.max(SocialPost.fetched_at)).group_by(SocialPost.platform)
    ).all()
    return {platform: as_utc(latest) for platform, latest in rows if latest is not None}


def _rows(frame: pd.DataFrame, fallback_fetched_at: datetime) -> Iterator[dict[str, Any]]:
    for record in frame.to_dict(orient="records"):
        row = {str(name): plain(value) for name, value in record.items()}
        if row.get("fetched_at") is None:
            row["fetched_at"] = fallback_fetched_at
        yield row


class SocialScraperConnector(Connector):
    """Upsert social-scraper's exported posts into ``social_posts``."""

    name = "social_scraper"

    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
        *,
        store: ObjectStore | None = None,
    ) -> None:
        super().__init__(settings, session_factory)
        self._store = store

    @property
    def store(self) -> ObjectStore:
        """The injected store, else the archive backend the settings configure."""
        if self._store is None:
            self._store = store_from_settings(self.settings)
        return self._store

    def fetch(
        self,
        platforms: Collection[str] | None = None,
        lookback: timedelta = DEFAULT_LOOKBACK,
        **kwargs,
    ) -> int:
        """Load new and refreshed posts; returns rows inserted or refreshed.

        ``platforms`` limits the run to e.g. ``["reddit"]``; ``None`` loads every platform
        the dataset carries. Raises :class:`SocialScraperDatasetMissing` when the archive has
        no ``social_scraper_posts`` manifest (see the module docstring for why that is an
        error rather than 0).
        """
        manifest = load_manifest(self.store, DATASET)
        if manifest is None:
            raise SocialScraperDatasetMissing(
                f"no {DATASET!r} manifest in the archive ({_describe(self.store)}): "
                "social-scraper has not exported to this store yet, or the archive settings "
                "point at a different tree or bucket"
            )
        wanted = set(platforms) if platforms is not None else None
        count = 0
        with self.session() as session:
            selected = select_partitions(manifest, _watermarks(session), wanted, lookback)
            for key, entry in selected:
                count += self._load_partition(session, key, entry, wanted, {})
                # Write the partition and let go of its posts before reading the next, so
                # memory stays one partition deep however many a run selects. Still one
                # transaction: a failure later in the run rolls this partition back too.
                session.flush()
                session.expunge_all()
        logger.info(
            "social-scraper: %d row(s) upserted from %d of %d partition(s)",
            count,
            len(selected),
            len(manifest.partitions),
        )
        return count

    def _load_partition(
        self,
        session: Session,
        key: str,
        entry: PartitionEntry,
        platforms: set[str] | None,
        known: dict[_Key, SocialPost],
    ) -> int:
        try:
            data = self.store.get_bytes(key)
        except KeyError:
            raise RuntimeError(
                f"{DATASET} manifest lists {key!r} but the archive has no such object; "
                "re-export it from social-scraper (nothing from this run was written)"
            ) from None
        rows = [
            row
            for row in _rows(parquet_bytes_to_frame(data), entry.updated_at)
            if platforms is None or row["platform"] in platforms
        ]
        _preload(session, rows, known)
        return sum(_upsert(session, row, known) for row in rows)


def _preload(session: Session, rows: list[dict[str, Any]], known: dict[_Key, SocialPost]) -> None:
    """Fetch the stored copies of ``rows`` in bounded ``IN`` batches, into ``known``."""
    by_platform: dict[str, list[str]] = {}
    for row in rows:
        if (row["platform"], row["external_id"]) not in known:
            by_platform.setdefault(row["platform"], []).append(row["external_id"])
    for platform, ids in by_platform.items():
        for batch in chunked(ids):
            stored = session.scalars(
                select(SocialPost).where(
                    SocialPost.platform == platform, SocialPost.external_id.in_(batch)
                )
            )
            for post in stored:
                known[(post.platform, post.external_id)] = post


def _upsert(session: Session, row: dict[str, Any], known: dict[_Key, SocialPost]) -> int:
    """Insert ``row``, or refresh the stored copy when ``row`` is newer. Returns 1 if written."""
    key = (row["platform"], row["external_id"])
    existing = known.get(key)
    if existing is None:
        post = SocialPost(
            platform=row["platform"],
            external_id=row["external_id"],
            created_at=row["created_at"],
            author_hash=row.get("author_hash"),
            sentiment=None,  # the consumer scores rows where this is NULL
        )
        _refresh(post, row)
        session.add(post)
        known[key] = post
        return 1
    if row["fetched_at"] <= as_utc(existing.fetched_at):
        return 0
    # Re-observed: refresh the volatile fields, keep first-seen created_at and author.
    _refresh(existing, row)
    return 1


def _refresh(post: SocialPost, row: dict[str, Any]) -> None:
    post.channel = row.get("channel") or ""
    post.title = row.get("title")
    post.body = row.get("body")
    post.score = _int(row.get("score"))
    post.num_comments = _int(row.get("num_comments"))
    post.symbols = row.get("symbols")
    post.raw = _raw(row)
    post.fetched_at = row["fetched_at"]


def _describe(store: ObjectStore) -> str:
    root = getattr(store, "root", None)
    if root is not None:
        return f"local dir {root}"
    bucket = getattr(store, "bucket", None)
    if bucket is not None:
        prefix = getattr(store, "prefix", "")
        return f"s3://{bucket}/{prefix}" if prefix else f"s3://{bucket}"
    return type(store).__name__
