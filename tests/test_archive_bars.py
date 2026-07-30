"""Price-bar archiving: only old intraday bars leave, uploads are verified before any
delete, partitions merge idempotently, restore is idempotent and rebuild-safe. Hermetic —
in-memory SQLite + a LocalDirStore in tmp_path, no network."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.archive.bars import archive_price_bars, restore_price_bars
from data_lake.archive.parquet_io import parquet_bytes_to_frame
from data_lake.archive.store import LocalDirStore
from data_lake.db.models import Base, Instrument, PriceBar

pytest.importorskip("pyarrow")

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _bar(instrument_id: int, ts: datetime, bar_size: str, close: float = 10.0) -> PriceBar:
    return PriceBar(
        instrument_id=instrument_id,
        ts=ts,
        bar_size=bar_size,
        source="ibkr",
        what_to_show="TRADES",
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=100.0,
    )


@pytest.fixture
def seeded():
    session = _session()
    session.add(Instrument(id=1, symbol="AAPL", exchange="SMART", currency="USD"))
    session.add(Instrument(id=2, symbol="RY", exchange="TSE", currency="CAD"))
    session.add_all(
        [
            # old minute bars (archivable), spanning two months and two instruments
            _bar(1, datetime(2024, 1, 15, 14, 30, tzinfo=UTC), "1 min", 11.0),
            _bar(1, datetime(2024, 1, 15, 14, 31, tzinfo=UTC), "1 min", 12.0),
            _bar(1, datetime(2024, 2, 1, 15, 0, tzinfo=UTC), "1 min", 13.0),
            _bar(2, datetime(2024, 1, 20, 16, 0, tzinfo=UTC), "1 min", 90.0),
            # old daily bar — must never be archived
            _bar(1, datetime(2024, 1, 15, tzinfo=UTC), "1 day", 11.5),
            # recent minute bar — inside the retention window
            _bar(1, datetime(2026, 6, 30, 14, 30, tzinfo=UTC), "1 min", 20.0),
        ]
    )
    session.commit()
    return session


def _bar_count(session: Session, bar_size: str | None = None) -> int:
    query = select(PriceBar)
    if bar_size:
        query = query.where(PriceBar.bar_size == bar_size)
    return len(session.execute(query).scalars().all())


def test_archive_moves_only_old_intraday_bars(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    result = archive_price_bars(seeded, store, older_than_days=365, now=NOW)

    assert result.rows_archived == result.rows_removed == 4
    assert set(result.objects) == {
        "price_bars/bar_size=1_min/2024-01.parquet",
        "price_bars/bar_size=1_min/2024-02.parquet",
    }
    # daily bar and the recent minute bar survive locally
    assert _bar_count(seeded, "1 day") == 1
    assert _bar_count(seeded, "1 min") == 1

    frame = parquet_bytes_to_frame(store.get_bytes("price_bars/bar_size=1_min/2024-01.parquet"))
    assert len(frame) == 3
    assert set(frame["symbol"]) == {"AAPL", "RY"}  # self-describing: symbol, not local id
    ry = frame[frame["symbol"] == "RY"].iloc[0]
    assert (ry["exchange"], ry["currency"], ry["close"]) == ("TSE", "CAD", 90.0)


def test_archive_refuses_daily_bars(seeded, tmp_path):
    with pytest.raises(ValueError, match="refusing to archive '1 day'"):
        archive_price_bars(seeded, LocalDirStore(tmp_path), bar_sizes=["1 day"], now=NOW)
    assert _bar_count(seeded) == 6  # nothing touched


def test_archive_is_idempotent_and_merges_partitions(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    archive_price_bars(seeded, store, older_than_days=365, now=NOW)
    # second run: nothing left to archive, partitions unchanged
    again = archive_price_bars(seeded, store, older_than_days=365, now=NOW)
    assert again.rows_archived == 0

    # a re-ingested duplicate of an archived bar merges without duplicating the partition row
    seeded.add(_bar(1, datetime(2024, 1, 15, 14, 30, tzinfo=UTC), "1 min", 999.0))
    seeded.commit()
    third = archive_price_bars(seeded, store, older_than_days=365, now=NOW)
    assert third.rows_archived == 1
    frame = parquet_bytes_to_frame(store.get_bytes("price_bars/bar_size=1_min/2024-01.parquet"))
    assert len(frame) == 3  # still three unique keys
    # first-archived row wins: archived data is immutable
    kept = frame[(frame["symbol"] == "AAPL") & (frame["close"] == 11.0)]
    assert len(kept) == 1


def test_failed_verification_keeps_local_rows(seeded, tmp_path, monkeypatch):
    store = LocalDirStore(tmp_path)
    # storage that silently drops writes: verification must catch it and nothing is deleted
    monkeypatch.setattr(store, "put_bytes", lambda key, data: None)
    with pytest.raises(RuntimeError, match="verification failed"):
        archive_price_bars(seeded, store, older_than_days=365, now=NOW)
    seeded.rollback()
    assert _bar_count(seeded) == 6


def test_restore_into_rebuilt_db_recreates_instruments(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    archive_price_bars(seeded, store, older_than_days=365, now=NOW)

    fresh = _session()  # rebuilt DB: no instruments, no bars
    count = restore_price_bars(fresh, store, start=date(2024, 1, 1), end=date(2024, 12, 31))
    assert count == 4
    ry = fresh.execute(select(Instrument).where(Instrument.symbol == "RY")).scalar_one()
    assert (ry.exchange, ry.currency) == ("TSE", "CAD")
    bars = fresh.execute(select(PriceBar)).scalars().all()
    assert {b.close for b in bars} == {11.0, 12.0, 13.0, 90.0}

    # idempotent: restoring again adds nothing
    assert restore_price_bars(fresh, store, start=date(2024, 1, 1), end=date(2024, 12, 31)) == 0


def test_restore_filters_by_range_and_bar_size(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    archive_price_bars(seeded, store, older_than_days=365, now=NOW)

    fresh = _session()
    count = restore_price_bars(
        fresh, store, start=date(2024, 1, 16), end=date(2024, 1, 31), bar_sizes=["1 min"]
    )
    assert count == 1  # only RY's Jan 20 bar is inside the window
    bar = fresh.execute(select(PriceBar)).scalar_one()
    assert bar.close == 90.0
    assert restore_price_bars(fresh, store, start=date(2024, 3, 1), end=date(2024, 3, 31)) == 0


def test_restore_rejects_inverted_range(seeded, tmp_path):
    with pytest.raises(ValueError, match="start must be <= end"):
        restore_price_bars(
            seeded, LocalDirStore(tmp_path), start=date(2024, 2, 1), end=date(2024, 1, 1)
        )


def test_archive_rejects_negative_age(seeded, tmp_path):
    with pytest.raises(ValueError, match="older_than_days"):
        archive_price_bars(seeded, LocalDirStore(tmp_path), older_than_days=-1)
