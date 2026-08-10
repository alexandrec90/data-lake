"""Tests for the read-only DuckDB research lens over archive Parquet."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from data_lake.archive import lens
from data_lake.archive.parquet_io import frame_to_parquet_bytes
from data_lake.archive.store import LocalDirStore
from data_lake.testing import lake_settings

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

BAR_COLUMNS = [
    "symbol",
    "exchange",
    "currency",
    "ts",
    "bar_size",
    "source",
    "what_to_show",
    "open",
    "high",
    "low",
    "close",
    "volume",
]
RAW_COLUMNS = ["source", "external_id", "event_ts", "fetched_at", "raw_json"]


@pytest.fixture
def local_archive(tmp_path):
    store = LocalDirStore(tmp_path)
    bars = pd.DataFrame(
        [
            {
                "symbol": "XEQT.TO",
                "exchange": "SMART",
                "currency": "CAD",
                "ts": datetime(2025, 1, 2, 14, 30, tzinfo=UTC),
                "bar_size": "1 min",
                "source": "ibkr",
                "what_to_show": "TRADES",
                "open": 31.0,
                "high": 31.2,
                "low": 30.9,
                "close": 31.1,
                "volume": 1000.0,
            },
            {
                "symbol": "RY",
                "exchange": "TSE",
                "currency": "CAD",
                "ts": datetime(2025, 1, 2, 14, 31, tzinfo=UTC),
                "bar_size": "1 min",
                "source": "ibkr",
                "what_to_show": "TRADES",
                "open": 170.0,
                "high": 171.0,
                "low": 169.5,
                "close": 170.5,
                "volume": 500.0,
            },
        ]
    )
    raw = pd.DataFrame(
        [
            {
                "source": "finnhub",
                "external_id": "news-1",
                "event_ts": datetime(2025, 1, 3, 12, tzinfo=UTC),
                "fetched_at": datetime(2025, 1, 3, 12, 5, tzinfo=UTC),
                "raw_json": '{"category": "company"}',
            }
        ]
    )
    store.put_bytes("price_bars/bar_size=1_min/2025-01.parquet", frame_to_parquet_bytes(bars))
    store.put_bytes("raw/news_articles/2025-01.parquet", frame_to_parquet_bytes(raw))
    return tmp_path


def _settings(path) -> object:
    return lake_settings(archive_backend="local", archive_local_dir=str(path))


def test_local_lens_exposes_documented_views_and_pushes_predicates(local_archive):
    connection = lens.connect_lens(_settings(local_archive))
    try:
        assert {row[0] for row in connection.execute("SHOW TABLES").fetchall()} == {
            "bars",
            "raw_payloads",
        }
        assert [row[0] for row in connection.execute("DESCRIBE bars").fetchall()] == BAR_COLUMNS
        assert [row[0] for row in connection.execute("DESCRIBE raw_payloads").fetchall()] == (
            RAW_COLUMNS
        )

        rows = connection.execute(
            "SELECT symbol, close FROM bars "
            "WHERE symbol = 'XEQT.TO' AND ts >= TIMESTAMPTZ '2025-01-02 14:30:00+00'"
        ).fetchall()
        assert rows == [("XEQT.TO", 31.1)]
        assert connection.execute(
            "SELECT external_id FROM raw_payloads WHERE source = 'finnhub'"
        ).fetchall() == [("news-1",)]

        plan = connection.execute(
            "EXPLAIN SELECT close FROM bars WHERE symbol = 'XEQT.TO'"
        ).fetchone()[1]
        assert "READ_PARQUET" in plan
        assert "symbol='XEQT.TO'" in plan
    finally:
        connection.close()


def test_local_lens_creates_typed_empty_views(tmp_path):
    connection = lens.connect_lens(_settings(tmp_path))
    try:
        assert connection.execute("SELECT count(*) FROM bars").fetchone() == (0,)
        assert [row[0] for row in connection.execute("DESCRIBE bars").fetchall()] == BAR_COLUMNS
        assert connection.execute("SELECT count(*) FROM raw_payloads").fetchone() == (0,)
    finally:
        connection.close()


class _FakeConnection:
    def __init__(self):
        self.commands: list[str] = []
        self.closed = False

    def execute(self, sql: str):
        self.commands.append(sql)
        return self

    def fetchone(self):
        return (True,)

    def close(self):
        self.closed = True


class _FakeDuckDB:
    def __init__(self, connection: _FakeConnection):
        self.connection = connection
        self.connect_calls: list[dict[str, str]] = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return self.connection


def test_s3_settings_create_scoped_temporary_secret_without_network(monkeypatch):
    connection = _FakeConnection()
    duckdb_module = _FakeDuckDB(connection)
    monkeypatch.setattr(lens, "_require_duckdb", lambda: duckdb_module)
    settings = lake_settings(
        archive_backend="s3",
        archive_s3_bucket="private-archive",
        archive_s3_prefix="owner/cold",
        archive_s3_endpoint_url="https://account.r2.cloudflarestorage.com",
        archive_s3_region="auto",
        archive_s3_access_key_id="test-key",
        archive_s3_secret_access_key="test-secret",  # pragma: allowlist secret - fixture, no bucket exists
    )

    returned = lens.connect_lens(settings)

    assert returned is connection
    assert duckdb_module.connect_calls == [{"database": ":memory:"}]
    assert connection.commands[:2] == ["INSTALL httpfs", "LOAD httpfs"]
    secret = connection.commands[2]
    assert "CREATE SECRET archive_lens" in secret
    assert "KEY_ID 'test-key'" in secret
    assert "SECRET 'test-secret'" in secret
    assert "REGION 'auto'" in secret
    assert "ENDPOINT 'account.r2.cloudflarestorage.com'" in secret
    assert "USE_SSL true" in secret
    assert "URL_STYLE 'path'" in secret
    assert "SCOPE 's3://private-archive/owner/cold'" in secret
    assert "SELECT EXISTS" in connection.commands[3]
    assert (
        "s3://private-archive/owner/cold/price_bars/bar_size=*/*.parquet"
        in (connection.commands[3])
    )
    assert "CREATE VIEW bars" in connection.commands[4]
    assert "SELECT EXISTS" in connection.commands[5]
    assert "s3://private-archive/owner/cold/raw/*/*.parquet" in connection.commands[5]
    assert "CREATE VIEW raw_payloads" in connection.commands[6]
    assert not connection.closed


def test_s3_empty_dataset_gets_typed_empty_view(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(connection, "fetchone", lambda: (False,))
    monkeypatch.setattr(lens, "_require_duckdb", lambda: _FakeDuckDB(connection))
    settings = lake_settings(
        archive_backend="s3",
        archive_s3_bucket="empty-archive",
        archive_s3_access_key_id="test-key",
        archive_s3_secret_access_key="test-secret",  # pragma: allowlist secret
    )

    lens.connect_lens(settings)

    assert "CREATE VIEW bars AS" in connection.commands[4]
    assert "read_parquet" not in connection.commands[4]
    assert "CREATE VIEW raw_payloads AS" in connection.commands[6]
    assert "read_parquet" not in connection.commands[6]
