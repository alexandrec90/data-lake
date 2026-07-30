"""The self-describing archive catalog: manifests record each partition's key, schema, row
count and time span; they are rebuildable from the partitions; and the archive writers keep
them in step with what they upload. Hermetic — in-memory SQLite + LocalDirStore, no network."""

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.archive import bars as bars_mod
from data_lake.archive import raw as raw_mod
from data_lake.archive.bars import archive_price_bars
from data_lake.archive.catalog import (
    CATALOG_PREFIX,
    DATASET_SPECS,
    list_datasets,
    load_catalog,
    load_manifest,
    manifest_key,
    rebuild_catalog,
    record_partition,
    spec_for,
)
from data_lake.archive.raw import archive_raw_payloads
from data_lake.archive.store import LocalDirStore
from data_lake.db.models import Base, Instrument, NewsArticle, PriceBar, SocialPost

pytest.importorskip("pyarrow")

NOW = datetime(2026, 7, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=400)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _bars_frame(times: list[datetime], symbol: str = "AAPL") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbol,
            "exchange": "SMART",
            "currency": "USD",
            "ts": pd.to_datetime(times, utc=True),
            "bar_size": "1 min",
            "source": "ibkr",
            "what_to_show": "TRADES",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100.0,
        }
    )


# --- unit: record_partition -------------------------------------------------------------


def test_record_partition_captures_key_schema_and_span(tmp_path):
    store = LocalDirStore(tmp_path)
    frame = _bars_frame([datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 20, tzinfo=UTC)])

    manifest = record_partition(
        store, spec_for("price_bars"), "price_bars/p.parquet", frame, now=NOW
    )

    assert manifest.dataset == "price_bars"
    assert manifest.ts_column == "ts"
    assert tuple(manifest.key_columns) == spec_for("price_bars").key_columns
    assert manifest.total_rows == 2
    assert manifest.min_ts == datetime(2024, 1, 2, tzinfo=UTC)
    assert manifest.max_ts == datetime(2024, 1, 20, tzinfo=UTC)
    assert "close" in manifest.schema and "_id" not in manifest.schema
    # persisted and re-readable
    assert load_manifest(store, "price_bars").total_rows == 2


def test_record_partition_upserts_and_aggregates_across_partitions(tmp_path):
    store = LocalDirStore(tmp_path)
    spec = spec_for("price_bars")
    record_partition(
        store,
        spec,
        "price_bars/jan.parquet",
        _bars_frame([datetime(2024, 1, 5, tzinfo=UTC)]),
        now=NOW,
    )
    record_partition(
        store,
        spec,
        "price_bars/feb.parquet",
        _bars_frame([datetime(2024, 2, 5, tzinfo=UTC)]),
        now=NOW,
    )

    manifest = load_manifest(store, "price_bars")
    assert set(manifest.partitions) == {"price_bars/jan.parquet", "price_bars/feb.parquet"}
    assert manifest.total_rows == 2
    assert manifest.min_ts == datetime(2024, 1, 5, tzinfo=UTC)
    assert manifest.max_ts == datetime(2024, 2, 5, tzinfo=UTC)

    # re-recording the same partition with more rows replaces (not doubles) its entry
    grown = _bars_frame([datetime(2024, 1, 5, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)])
    record_partition(store, spec, "price_bars/jan.parquet", grown, now=NOW)
    assert load_manifest(store, "price_bars").partitions["price_bars/jan.parquet"].rows == 2
    assert load_manifest(store, "price_bars").total_rows == 3


def test_load_helpers_on_empty_and_populated_store(tmp_path):
    store = LocalDirStore(tmp_path)
    assert load_manifest(store, "price_bars") is None
    assert list_datasets(store) == []
    assert load_catalog(store) == {}

    record_partition(
        store, spec_for("price_bars"), "price_bars/p.parquet", _bars_frame([NOW]), now=NOW
    )
    assert list_datasets(store) == ["price_bars"]
    assert set(load_catalog(store)) == {"price_bars"}


def test_manifest_is_valid_json_under_catalog_prefix(tmp_path):
    store = LocalDirStore(tmp_path)
    record_partition(
        store, spec_for("price_bars"), "price_bars/p.parquet", _bars_frame([NOW]), now=NOW
    )
    payload = json.loads(store.get_bytes(manifest_key("price_bars")))
    assert payload["dataset"] == "price_bars"
    assert manifest_key("price_bars").startswith(CATALOG_PREFIX)


# --- integration: archive writers keep the catalog in step ------------------------------


def _seed_bars(session: Session) -> None:
    session.add(Instrument(id=1, symbol="AAPL", exchange="SMART", currency="USD"))
    session.add_all(
        [
            PriceBar(
                instrument_id=1,
                ts=datetime(2024, 1, 15, 14, 30, tzinfo=UTC),
                bar_size="1 min",
                source="ibkr",
                what_to_show="TRADES",
                open=1,
                high=2,
                low=0.5,
                close=1.5,
                volume=10.0,
            ),
            PriceBar(
                instrument_id=1,
                ts=datetime(2024, 2, 1, 14, 30, tzinfo=UTC),
                bar_size="1 min",
                source="ibkr",
                what_to_show="TRADES",
                open=1,
                high=2,
                low=0.5,
                close=1.5,
                volume=10.0,
            ),
        ]
    )
    session.commit()


def test_archive_price_bars_writes_catalog(tmp_path):
    session = _session()
    _seed_bars(session)
    store = LocalDirStore(tmp_path)

    archive_price_bars(session, store, older_than_days=365, now=NOW)

    manifest = load_manifest(store, "price_bars")
    assert set(manifest.partitions) == {
        "price_bars/bar_size=1_min/2024-01.parquet",
        "price_bars/bar_size=1_min/2024-02.parquet",
    }
    assert manifest.total_rows == 2
    assert "_id" not in manifest.schema  # the local-only id never reaches the catalog


def test_archive_raw_payloads_writes_catalog(tmp_path):
    session = _session()
    session.add(
        NewsArticle(
            source="finnhub",
            external_id="n1",
            published_at=OLD,
            title="t",
            summary="s",
            sentiment=0.3,
            raw={"id": 1},
            fetched_at=OLD,
        )
    )
    session.add(
        SocialPost(
            platform="reddit",
            channel="stocks",
            external_id="s1",
            created_at=OLD,
            author_hash="a" * 64,
            title="t",
            body="b",
            sentiment=-0.1,
            raw={"x": 1},
            fetched_at=OLD,
        )
    )
    session.commit()
    store = LocalDirStore(tmp_path)

    archive_raw_payloads(session, store, min_age_days=30, now=NOW)

    catalog = load_catalog(store)
    assert set(catalog) == {"news_articles", "social_posts"}
    assert catalog["news_articles"].total_rows == 1
    assert tuple(catalog["social_posts"].key_columns) == ("source", "external_id")


# --- rebuild ----------------------------------------------------------------------------


def test_rebuild_reconstructs_catalog_from_partitions(tmp_path):
    session = _session()
    _seed_bars(session)
    store = LocalDirStore(tmp_path)
    archive_price_bars(session, store, older_than_days=365, now=NOW)

    # wipe the manifests; the partitions themselves remain the source of truth
    for obj in store.list_objects(CATALOG_PREFIX):
        (tmp_path / obj.key).unlink()
    assert load_catalog(store) == {}

    rebuilt = rebuild_catalog(store, now=NOW)
    assert set(rebuilt["price_bars"].partitions) == {
        "price_bars/bar_size=1_min/2024-01.parquet",
        "price_bars/bar_size=1_min/2024-02.parquet",
    }
    assert load_manifest(store, "price_bars").total_rows == 2


def test_rebuild_drops_partitions_that_no_longer_exist(tmp_path):
    store = LocalDirStore(tmp_path)
    # a stale manifest referencing a partition object that was never written
    record_partition(
        store,
        spec_for("price_bars"),
        "price_bars/bar_size=1_min/1999-01.parquet",
        _bars_frame([datetime(1999, 1, 1, tzinfo=UTC)]),
        now=NOW,
    )
    assert load_manifest(store, "price_bars").total_rows == 1

    # nothing is actually stored under the data prefix, so a rebuild finds no partitions
    rebuilt = rebuild_catalog(store, now=NOW)
    assert "price_bars" not in rebuilt


# --- drift guard: the catalog must advertise the writers' real natural keys -------------


def test_dataset_specs_match_the_archive_writers():
    assert spec_for("price_bars").key_columns == bars_mod._BAR_KEY
    assert spec_for("price_bars").ts_column == "ts"
    assert spec_for("news_articles").key_columns == raw_mod._RAW_KEY
    assert spec_for("social_posts").key_columns == raw_mod._RAW_KEY
    # every spec is uniquely named
    assert len({spec.name for spec in DATASET_SPECS}) == len(DATASET_SPECS)


def test_spec_for_rejects_unknown_dataset():
    with pytest.raises(KeyError, match="unknown archive dataset"):
        spec_for("options_chains")


def test_rebuild_ignores_non_parquet_objects_under_a_data_prefix(tmp_path):
    session = _session()
    _seed_bars(session)
    store = LocalDirStore(tmp_path)
    archive_price_bars(session, store, older_than_days=365, now=NOW)
    # a stray non-parquet object under the data prefix must not be catalogued as a partition
    store.put_bytes("price_bars/bar_size=1_min/_SUCCESS", b"marker")

    rebuilt = rebuild_catalog(store, now=NOW)
    assert set(rebuilt["price_bars"].partitions) == {
        "price_bars/bar_size=1_min/2024-01.parquet",
        "price_bars/bar_size=1_min/2024-02.parquet",
    }
