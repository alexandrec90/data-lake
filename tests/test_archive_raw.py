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


def _posts_across_two_months(session: Session) -> list[str]:
    """Five scored, aged posts over two event months, inserted out of event order."""
    later = OLD + timedelta(days=40)
    session.add_all(
        [
            _post("b1", 0.1, later, raw={"n": 1}),
            _post("a1", 0.1, OLD, raw={"n": 2}),
            _post("b2", 0.1, later, raw={"n": 3}),
            _post("a2", 0.1, OLD, raw={"n": 4}),
            _post("a3", 0.1, OLD, raw={"n": 5}),
        ]
    )
    session.commit()
    return [f"raw/social_posts/{OLD:%Y-%m}.parquet", f"raw/social_posts/{later:%Y-%m}.parquet"]


def test_a_backlog_is_archived_in_bounded_batches_each_committed(tmp_path, monkeypatch):
    """ibkr_trader's scheduler read 8.1M social posts into memory in one SELECT and was
    OOM-killed within seconds, on every restart, so nothing was ever archived. A batch is
    at most `batch_rows` rows, read as columns, and committed before the next is read."""
    session = _session()
    month_keys = _posts_across_two_months(session)
    store = LocalDirStore(tmp_path)
    commits: list[int] = []
    real_commit = session.commit
    monkeypatch.setattr(
        session,
        "commit",
        lambda: (commits.append(len(session.identity_map)), real_commit())[1],
    )

    result = archive_raw_payloads(session, store, now=NOW, batch_rows=2)

    assert result.rows_archived == result.rows_removed == 5
    assert len(commits) == 3, "five rows in batches of two"
    assert commits == [0, 0, 0], "no ORM row is held while a batch is written"
    assert set(result.objects) == set(month_keys)
    archived = parquet_bytes_to_frame(store.get_bytes(month_keys[0]))
    assert sorted(archived["external_id"]) == ["a1", "a2", "a3"], "batches merge into the month"
    assert all(post.raw is None for post in session.execute(select(SocialPost)).scalars())


def test_a_run_that_fails_partway_keeps_the_batches_it_finished(tmp_path, monkeypatch):
    """Committed per batch: a backlog of millions is many runs' worth if a run dies, and
    each run resumes where the last stopped instead of starting over."""
    session = _session()
    _posts_across_two_months(session)
    store = LocalDirStore(tmp_path)
    import data_lake.archive.raw as raw_module

    real_verify = raw_module.verify_partition
    calls: list[str] = []

    def verify_twice(*args, **kwargs):
        calls.append(args[1])
        if len(calls) > 1:
            raise RuntimeError("archive verification failed: injected")
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(raw_module, "verify_partition", verify_twice)
    with pytest.raises(RuntimeError, match="injected"):
        archive_raw_payloads(session, store, now=NOW, batch_rows=2)
    session.rollback()
    nulled = {p.external_id for p in session.execute(select(SocialPost)).scalars() if p.raw is None}
    assert nulled == {"a1", "a2"}, "the first batch stays archived; the failed one is untouched"

    monkeypatch.setattr(raw_module, "verify_partition", real_verify)
    again = archive_raw_payloads(session, store, now=NOW, batch_rows=2)
    assert again.rows_archived == 3


def test_a_json_null_payload_never_stalls_the_batches(tmp_path):
    """A JSON 'null' payload passes `raw IS NOT NULL` and is never NULLed, so a batch of
    nothing but those must still move the read past them."""
    session = _session()
    session.add_all([_post(f"j{i}", 0.1, OLD, raw=None) for i in range(3)])
    session.add(_post("z", 0.1, OLD + timedelta(days=1), raw={"n": 1}))
    session.commit()
    result = archive_raw_payloads(session, LocalDirStore(tmp_path), now=NOW, batch_rows=2)
    assert result.rows_archived == 1


def test_batch_rows_must_be_positive():
    with pytest.raises(ValueError, match="batch_rows"):
        archive_raw_payloads(_session(), LocalDirStore("unused"), batch_rows=0)


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
