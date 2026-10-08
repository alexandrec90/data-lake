"""SocialScraperConnector: social-scraper's lake export -> ``social_posts``.

Hermetic: a LocalDirStore in tmp_path holding real Parquet plus a ``_catalog/`` manifest
written through this package's own archive layer (the same manifest shape social-scraper
writes), and in-memory SQLite injected per call.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.archive.catalog import DatasetManifest, DatasetSpec, PartitionEntry, record_partition
from data_lake.archive.parquet_io import frame_to_parquet_bytes, parquet_bytes_to_frame
from data_lake.archive.store import LocalDirStore, S3ObjectStore
from data_lake.db.models import Base, SocialPost
from data_lake.ingestion.base import stable_hash
from data_lake.ingestion.social import social_scraper
from data_lake.ingestion.social.social_scraper import (
    DATASET,
    SocialScraperConnector,
    SocialScraperDatasetMissing,
    partition_platform,
    plain,
    select_partitions,
)
from data_lake.testing import lake_settings

pytest.importorskip("pyarrow")

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CREATED = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)

#: social-scraper's catalog contract, expressed with this package's own DatasetSpec.
SPEC = DatasetSpec(
    name=DATASET,
    prefix="social_scraper/posts/",
    ts_column="created_at",
    key_columns=("platform", "external_id"),
)


def _session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def _post(platform: str, external_id: str, *, fetched: datetime, **fields) -> dict:
    row = {
        "platform": platform,
        "channel": "stocks" if platform == "reddit" else "search",
        "external_id": external_id,
        "kind": "submission",
        "created_at": CREATED,
        "fetched_at": fetched,
        "author_hash": stable_hash("someone"),
        "title": f"title {external_id}",
        "body": f"body {external_id}",
        "url": f"https://example.invalid/{external_id}",
        "flair": "DD",
        "symbols": ["AAPL"],
        "score": 10,
        "num_comments": 2,
        "likes": None,
        "stance": 0.5,
    }
    row.update(fields)
    return row


def _partition_key(row: dict) -> str:
    return f"social_scraper/posts/platform={row['platform']}/dt={row['created_at']:%Y-%m-%d}/part.parquet"


def _export(store: LocalDirStore, rows: list[dict], now: datetime) -> list[str]:
    """Write ``rows`` the way social-scraper's exporter does: merge (new rows win), catalog."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(_partition_key(row), []).append(row)
    for key, group in groups.items():
        new = pd.DataFrame(group)
        if store.exists(key):
            new = pd.concat([parquet_bytes_to_frame(store.get_bytes(key)), new], ignore_index=True)
        merged = new.drop_duplicates(subset=list(SPEC.key_columns), keep="last")
        merged = merged.reset_index(drop=True)
        for column in ("created_at", "fetched_at"):
            merged[column] = pd.to_datetime(merged[column], utc=True)
        store.put_bytes(key, frame_to_parquet_bytes(merged))
        record_partition(store, SPEC, key, merged, now=now)
    return sorted(groups)


def _posts(session_factory) -> list[SocialPost]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(SocialPost).order_by(SocialPost.platform, SocialPost.external_id)
            )
        )


def _connector(store, session_factory) -> SocialScraperConnector:
    return SocialScraperConnector(session_factory=session_factory, store=store)


def test_rows_land_in_social_posts_column_for_column(tmp_path):
    store, factory = LocalDirStore(tmp_path), _session_factory()
    _export(
        store,
        [
            _post("reddit", "abc123", fetched=T0 - timedelta(minutes=5)),
            _post(
                "reddit",
                "t1_def456",
                fetched=T0 - timedelta(minutes=4),
                kind="comment",
                title=None,
                score=None,
                symbols=None,
            ),
        ],
        now=T0,
    )

    assert _connector(store, factory).fetch() == 2

    submission, comment = sorted(_posts(factory), key=lambda p: p.external_id)
    assert submission.platform == "reddit"
    assert submission.channel == "stocks"
    assert submission.external_id == "abc123"
    assert submission.created_at.replace(tzinfo=UTC) == CREATED
    assert submission.fetched_at.replace(tzinfo=UTC) == T0 - timedelta(minutes=5)
    assert submission.author_hash == stable_hash("someone")
    assert (submission.title, submission.body) == ("title abc123", "body abc123")
    assert (submission.score, submission.num_comments) == (10, 2)
    assert submission.symbols == ["AAPL"]
    assert submission.sentiment is None  # left for the consumer to score
    assert submission.raw == {"kind": "submission", "flair": "DD", "stance": 0.5}
    assert "url" not in submission.raw
    assert (comment.title, comment.score, comment.symbols) == (None, None, None)
    assert comment.raw["kind"] == "comment"


def test_both_platforms_load_and_can_be_limited(tmp_path):
    store = LocalDirStore(tmp_path)
    keys = _export(
        store,
        [
            _post("reddit", "r1", fetched=T0 - timedelta(minutes=5)),
            _post("x", "1790000000000000001", fetched=T0 - timedelta(minutes=5), score=None),
        ],
        now=T0,
    )
    assert [partition_platform(key) for key in keys] == ["reddit", "x"]

    only_x = _session_factory()
    assert _connector(store, only_x).fetch(platforms=["x"]) == 1
    assert [(p.platform, p.channel) for p in _posts(only_x)] == [("x", "search")]

    both = _session_factory()
    assert _connector(store, both).fetch() == 2
    assert [p.platform for p in _posts(both)] == ["reddit", "x"]


def test_rerun_refreshes_volatile_fields_without_duplicating(tmp_path):
    store, factory = LocalDirStore(tmp_path), _session_factory()
    _export(store, [_post("reddit", "abc", fetched=T0 - timedelta(minutes=5))], now=T0)
    assert _connector(store, factory).fetch() == 1
    with factory() as session:
        session.scalars(select(SocialPost)).one().sentiment = 0.25  # scored meanwhile

    later = T0 + timedelta(minutes=30)
    _export(
        store,
        [
            _post(
                "reddit",
                "abc",
                fetched=later - timedelta(minutes=5),
                score=99,
                num_comments=40,
                body="edited",
                created_at=CREATED,
            )
        ],
        now=later,
    )
    assert _connector(store, factory).fetch() == 1

    (post,) = _posts(factory)
    assert (post.score, post.num_comments, post.body) == (99, 40, "edited")
    assert post.fetched_at.replace(tzinfo=UTC) == later - timedelta(minutes=5)
    assert post.created_at.replace(tzinfo=UTC) == CREATED
    assert post.sentiment == 0.25  # a refresh never discards a score

    # Nothing newer in the archive: the partition may be re-read, but nothing is written.
    assert _connector(store, factory).fetch() == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SocialPost)) == 1


def test_older_copy_never_overwrites_a_newer_stored_row(tmp_path):
    store, factory = LocalDirStore(tmp_path), _session_factory()
    _export(store, [_post("reddit", "abc", fetched=T0 - timedelta(minutes=5), score=3)], now=T0)
    with factory() as session:  # e.g. a row the retired PRAW connector stored more recently
        session.add(
            SocialPost(
                platform="reddit",
                channel="stocks",
                external_id="abc",
                created_at=CREATED,
                score=50,
                fetched_at=T0,
            )
        )
    assert _connector(store, factory).fetch() == 0
    assert _posts(factory)[0].score == 50


def test_incremental_fetch_skips_partitions_already_loaded(tmp_path, monkeypatch):
    store, factory = LocalDirStore(tmp_path), _session_factory()
    day = timedelta(days=1)
    (first,) = _export(store, [_post("reddit", "a", fetched=T0 - timedelta(minutes=5))], now=T0)
    assert _connector(store, factory).fetch() == 1

    t1 = T0 + timedelta(hours=3)
    (second,) = _export(
        store,
        [_post("reddit", "b", fetched=t1 - timedelta(minutes=5), created_at=CREATED + day)],
        now=t1,
    )
    assert _connector(store, factory).fetch() == 1

    t2 = T0 + timedelta(hours=6)
    (third,) = _export(
        store,
        [_post("reddit", "c", fetched=t2 - timedelta(minutes=5), created_at=CREATED + 2 * day)],
        now=t2,
    )
    read: list[str] = []
    real_get = store.get_bytes

    def spy(key: str) -> bytes:
        read.append(key)
        return real_get(key)

    monkeypatch.setattr(store, "get_bytes", spy)
    assert _connector(store, factory).fetch() == 1

    parquet_reads = [key for key in read if key.endswith(".parquet")]
    assert first not in parquet_reads  # exported well before the watermark
    assert parquet_reads == [second, third]  # within the lookback, then the new one
    assert [p.external_id for p in _posts(factory)] == ["a", "b", "c"]


def test_a_loaded_partition_is_released_before_the_next_is_read(tmp_path, monkeypatch):
    """Memory stays one partition deep, however many partitions a run selects.

    Every post of every selected partition used to stay referenced until the run ended,
    which grew ibkr_trader's scheduler to three quarters of its 3.8 GB Docker VM.
    """
    store, factory = LocalDirStore(tmp_path), _session_factory()
    day = timedelta(days=1)
    keys = _export(
        store,
        [
            _post("reddit", "a", fetched=T0 - timedelta(minutes=5)),
            _post("reddit", "b", fetched=T0 - timedelta(minutes=5), created_at=CREATED + day),
            _post("x", "c", fetched=T0 - timedelta(minutes=5), created_at=CREATED + 2 * day),
        ],
        now=T0,
    )
    sessions: list[Session] = []

    @contextmanager
    def spying_factory() -> Iterator[Session]:
        with factory() as session:
            sessions.append(session)
            yield session

    held_at_read: dict[str, int] = {}
    real_get = store.get_bytes

    def spy(key: str) -> bytes:
        if key.endswith(".parquet"):
            held_at_read[key] = len(list(sessions[-1]))
        return real_get(key)

    monkeypatch.setattr(store, "get_bytes", spy)
    assert _connector(store, spying_factory).fetch() == 3

    assert held_at_read == dict.fromkeys(keys, 0)
    assert [p.external_id for p in _posts(factory)] == ["a", "b", "c"]


def test_select_partitions_holds_each_platform_to_its_own_watermark():
    def entry(updated_at: datetime) -> PartitionEntry:
        return PartitionEntry(rows=1, min_ts=CREATED, max_ts=CREATED, updated_at=updated_at)

    manifest = DatasetManifest(
        dataset=DATASET,
        ts_column="created_at",
        key_columns=["platform", "external_id"],
        partitions={
            "social_scraper/posts/platform=reddit/dt=2026-09-01/part.parquet": entry(T0),
            "social_scraper/posts/platform=reddit/dt=2026-10-01/part.parquet": entry(
                T0 + timedelta(hours=5)
            ),
            "social_scraper/posts/platform=x/dt=2026-09-01/part.parquet": entry(T0),
            "social_scraper/posts/odd/part.parquet": entry(T0 - timedelta(days=30)),
        },
    )
    watermarks = {"reddit": T0 + timedelta(hours=4)}  # nothing from x loaded yet

    chosen = select_partitions(manifest, watermarks, None, timedelta(hours=1))
    assert [key for key, _ in chosen] == [
        "social_scraper/posts/odd/part.parquet",  # no platform: always read, never lost
        "social_scraper/posts/platform=x/dt=2026-09-01/part.parquet",
        "social_scraper/posts/platform=reddit/dt=2026-10-01/part.parquet",
    ]

    only_reddit = select_partitions(manifest, watermarks, {"reddit"}, timedelta(hours=1))
    assert [partition_platform(key) for key, _ in only_reddit] == [None, "reddit"]


def test_missing_dataset_raises_naming_the_store(tmp_path):
    connector = _connector(LocalDirStore(tmp_path), _session_factory())
    with pytest.raises(SocialScraperDatasetMissing, match="social_scraper_posts") as raised:
        connector.fetch()
    assert str(tmp_path) in str(raised.value)
    assert isinstance(raised.value, RuntimeError)  # a consumer's job health sees a failure


def test_listed_partition_missing_from_store_raises_and_writes_nothing(tmp_path):
    store, factory = LocalDirStore(tmp_path), _session_factory()
    _export(store, [_post("reddit", "a", fetched=T0 - timedelta(minutes=5))], now=T0)
    (gone,) = _export(
        store,
        [_post("x", "1", fetched=T0 - timedelta(minutes=5))],
        now=T0 + timedelta(minutes=1),
    )
    (tmp_path / gone).unlink()

    with pytest.raises(RuntimeError, match="no such object"):
        _connector(store, factory).fetch()
    assert _posts(factory) == []  # the earlier partition rolled back with it


class _NoSuchKey(Exception):
    """Shaped like botocore's ClientError for a missing object."""

    response = {"Error": {"Code": "NoSuchKey"}}


class _FakeS3:
    """Records the bucket keys an S3ObjectStore asks for; the bucket is empty."""

    def __init__(self):
        self.requested: list[str] = []

    def get_object(self, Bucket: str, Key: str):
        self.requested.append(Key)
        raise _NoSuchKey(Key)


def test_s3_store_reads_the_manifest_under_the_configured_prefix():
    client = _FakeS3()
    connector = _connector(S3ObjectStore(client, "lake", prefix="/pooled/"), _session_factory())
    with pytest.raises(SocialScraperDatasetMissing, match="s3://lake/pooled"):
        connector.fetch()
    assert client.requested == ["pooled/_catalog/social_scraper_posts.json"]


def test_store_defaults_to_the_configured_archive_backend(tmp_path):
    local = SocialScraperConnector(
        lake_settings(archive_backend="local", archive_local_dir=str(tmp_path))
    )
    assert isinstance(local.store, LocalDirStore)
    assert local.store.root == tmp_path

    unconfigured = SocialScraperConnector(lake_settings(archive_backend="none"))
    with pytest.raises(RuntimeError, match="no archive backend configured"):
        unconfigured.fetch()


def test_describe_falls_back_to_the_store_type():
    class Bare:
        pass

    assert social_scraper._describe(Bare()) == "Bare"
    assert social_scraper._describe(S3ObjectStore(object(), "lake")) == "s3://lake"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (pd.NaT, None),
        (float("nan"), None),
        (np.float64("nan"), None),
        (np.int64(7), 7),
        (np.bool_(True), True),
        (np.array(["AAPL", "MSFT"], dtype=object), ["AAPL", "MSFT"]),
        (pd.Timestamp("2026-10-01T12:00:00Z"), T0),
        (datetime(2026, 10, 1, 12, 0), T0),  # naive is UTC
        ("text", "text"),
    ],
)
def test_plain_turns_parquet_cells_into_python(value, expected):
    assert plain(value) == expected


@pytest.mark.parametrize(
    ("key", "platform"),
    [
        ("social_scraper/posts/platform=reddit/dt=2026-10-01/part.parquet", "reddit"),
        ("platform=x/dt=2026-10-01/part.parquet", "x"),
        ("social_scraper/posts/part.parquet", None),
        ("social_scraper/posts/myplatform=x/part.parquet", None),
    ],
)
def test_partition_platform(key, platform):
    assert partition_platform(key) == platform
