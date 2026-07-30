"""Raw-payload archiving: only scored+aged blobs are offloaded, rows themselves never move,
verification gates the NULLing, and restore refills without overwriting. Hermetic —
in-memory SQLite + LocalDirStore, no network."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.archive.parquet_io import parquet_bytes_to_frame
from data_lake.archive.raw import archive_raw_payloads, restore_raw_payloads
from data_lake.archive.store import LocalDirStore
from data_lake.db.models import Base, NewsArticle, SocialPost

pytest.importorskip("pyarrow")

NOW = datetime(2026, 7, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=400)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _article(external_id: str, sentiment, ts: datetime, raw=None) -> NewsArticle:
    return NewsArticle(
        source="finnhub",
        external_id=external_id,
        published_at=ts,
        title="t",
        summary="s",
        sentiment=sentiment,
        raw=raw,
        fetched_at=ts,
    )


def _post(external_id: str, sentiment, ts: datetime, raw=None) -> SocialPost:
    return SocialPost(
        platform="reddit",
        channel="stocks",
        external_id=external_id,
        created_at=ts,
        author_hash="a" * 64,
        title="t",
        body="b",
        sentiment=sentiment,
        raw=raw,
        fetched_at=ts,
    )


@pytest.fixture
def seeded():
    session = _session()
    session.add_all(
        [
            _article("scored-old", 0.4, OLD, raw={"category": "company", "id": 1}),
            _article("unscored-old", None, OLD, raw={"id": 2}),  # must keep raw: not scored
            _article("scored-fresh", 0.2, NOW, raw={"id": 3}),  # must keep raw: too young
            _post("p-scored-old", -0.1, OLD, raw={"permalink": "/r/x/1"}),
            _post("p-already-null", 0.9, OLD, raw=None),  # nothing to archive
        ]
    )
    session.commit()
    return session


def test_archive_nulls_only_scored_aged_raw(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    result = archive_raw_payloads(seeded, store, min_age_days=30, now=NOW)

    assert result.rows_archived == result.rows_removed == 2
    month = f"{OLD:%Y-%m}"
    assert set(result.objects) == {
        f"raw/news_articles/{month}.parquet",
        f"raw/social_posts/{month}.parquet",
    }

    by_id = {a.external_id: a for a in seeded.execute(select(NewsArticle)).scalars()}
    assert by_id["scored-old"].raw is None  # offloaded
    assert by_id["scored-old"].title == "t"  # the row itself stayed
    assert by_id["unscored-old"].raw == {"id": 2}  # unscored: untouched
    assert by_id["scored-fresh"].raw == {"id": 3}  # too young: untouched

    frame = parquet_bytes_to_frame(store.get_bytes(f"raw/news_articles/{month}.parquet"))
    assert frame.iloc[0]["raw_json"] == '{"category": "company", "id": 1}'


def test_archive_is_idempotent(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    archive_raw_payloads(seeded, store, now=NOW)
    again = archive_raw_payloads(seeded, store, now=NOW)
    assert again.rows_archived == 0


def test_failed_verification_keeps_raw(seeded, tmp_path, monkeypatch):
    store = LocalDirStore(tmp_path)
    monkeypatch.setattr(store, "put_bytes", lambda key, data: None)  # storage drops writes
    with pytest.raises(RuntimeError, match="verification failed"):
        archive_raw_payloads(seeded, store, now=NOW)
    seeded.rollback()
    scored = seeded.execute(
        select(NewsArticle).where(NewsArticle.external_id == "scored-old")
    ).scalar_one()
    assert scored.raw == {"category": "company", "id": 1}


def test_restore_refills_only_null_raw(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    archive_raw_payloads(seeded, store, now=NOW)

    window = (OLD.date().replace(day=1), OLD.date() + timedelta(days=31))
    counts = restore_raw_payloads(seeded, store, start=window[0], end=window[1])
    assert counts == {"news_articles": 1, "social_posts": 1}

    article = seeded.execute(
        select(NewsArticle).where(NewsArticle.external_id == "scored-old")
    ).scalar_one()
    assert article.raw == {"category": "company", "id": 1}
    post = seeded.execute(
        select(SocialPost).where(SocialPost.external_id == "p-scored-old")
    ).scalar_one()
    assert post.raw == {"permalink": "/r/x/1"}

    # idempotent: populated payloads are never re-restored (or overwritten)
    again = restore_raw_payloads(seeded, store, start=window[0], end=window[1])
    assert again == {"news_articles": 0, "social_posts": 0}


def test_restore_outside_range_is_a_noop(seeded, tmp_path):
    store = LocalDirStore(tmp_path)
    archive_raw_payloads(seeded, store, now=NOW)
    counts = restore_raw_payloads(seeded, store, start=date(2010, 1, 1), end=date(2010, 12, 31))
    assert counts == {"news_articles": 0, "social_posts": 0}


def test_validation_errors():
    session = _session()
    with pytest.raises(ValueError, match="min_age_days"):
        archive_raw_payloads(session, LocalDirStore("unused"), min_age_days=-1)
    with pytest.raises(ValueError, match="start must be <= end"):
        restore_raw_payloads(
            session, LocalDirStore("unused"), start=date(2024, 2, 1), end=date(2024, 1, 1)
        )
