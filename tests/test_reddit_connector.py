from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from _lake_env import SETTINGS
from data_lake.db.models import Base, SocialPost
from data_lake.ingestion.base import stable_hash
from data_lake.ingestion.social import reddit


class FakeAuthor:
    def __init__(self, name: str):
        self.name = name


class FakeSubmission:
    def __init__(
        self,
        id: str,
        title: str,
        selftext: str,
        score: int,
        num_comments: int,
        created_utc: float,
        author,
    ):
        self.id = id
        self.title = title
        self.selftext = selftext
        self.score = score
        self.num_comments = num_comments
        self.created_utc = created_utc
        self.author = author
        self.permalink = f"/r/x/{id}"
        self.url = f"https://reddit.com/{id}"
        self.upvote_ratio = 0.9
        self.is_self = True
        self.link_flair_text = "DD"


class FakeSubreddit:
    def __init__(self, submissions: list[FakeSubmission]):
        self._submissions = submissions

    def new(self, limit: int):
        return list(self._submissions)[:limit]


class FakeReddit:
    def __init__(self, by_sub: dict[str, list[FakeSubmission]]):
        self._by_sub = by_sub

    def subreddit(self, name: str) -> FakeSubreddit:
        return FakeSubreddit(self._by_sub.get(name, []))


def _make_session_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


def _wire(monkeypatch, session_cm, reddit_client):
    monkeypatch.setattr(SETTINGS, "reddit_client_id", "id")
    monkeypatch.setattr(SETTINGS, "reddit_client_secret", "secret")
    monkeypatch.setattr(reddit, "_reddit_client", lambda settings: reddit_client)


def test_reddit_fetch_upserts_posts_and_hashes_author(monkeypatch):
    session_cm = _make_session_scope()
    client = FakeReddit(
        {
            "stocks": [
                FakeSubmission(
                    id="abc",
                    title="AAPL to the moon",
                    selftext="body text",
                    score=10,
                    num_comments=3,
                    created_utc=1_700_000_000.0,
                    author=FakeAuthor("alice"),
                )
            ]
        }
    )
    _wire(monkeypatch, session_cm, client)

    count = reddit.RedditConnector(session_factory=session_cm).fetch(
        subreddits=["stocks"], limit=50
    )

    assert count == 1
    with session_cm() as session:
        post = session.scalar(select(SocialPost))
        assert post is not None
        assert post.platform == "reddit"
        assert post.channel == "stocks"
        assert post.external_id == "abc"
        assert post.title == "AAPL to the moon"
        assert post.body == "body text"
        assert post.score == 10
        assert post.num_comments == 3
        # username is hashed, never stored plaintext
        assert post.author_hash == stable_hash("alice")
        assert "alice" not in str(post.author_hash)
        # raw is trimmed, not the whole submission
        assert set(post.raw) == {
            "permalink",
            "url",
            "upvote_ratio",
            "is_self",
            "link_flair_text",
        }


def test_reddit_repoll_updates_score_and_is_idempotent(monkeypatch):
    session_cm = _make_session_scope()

    def sub(score, num_comments):
        return FakeSubmission(
            id="abc",
            title="AAPL to the moon",
            selftext="body",
            score=score,
            num_comments=num_comments,
            created_utc=1_700_000_000.0,
            author=FakeAuthor("alice"),
        )

    _wire(monkeypatch, session_cm, FakeReddit({"stocks": [sub(10, 3)]}))
    assert reddit.RedditConnector(session_factory=session_cm).fetch(subreddits=["stocks"]) == 1

    # second poll: same id, higher score/comments
    _wire(monkeypatch, session_cm, FakeReddit({"stocks": [sub(25, 8)]}))
    assert reddit.RedditConnector(session_factory=session_cm).fetch(subreddits=["stocks"]) == 1

    with session_cm() as session:
        posts = session.scalars(select(SocialPost)).all()
        assert len(posts) == 1  # upsert, not duplicate
        assert posts[0].score == 25
        assert posts[0].num_comments == 8


def test_reddit_deleted_author_stores_null_hash(monkeypatch):
    session_cm = _make_session_scope()
    client = FakeReddit(
        {
            "stocks": [
                FakeSubmission(
                    id="abc",
                    title="t",
                    selftext="",
                    score=1,
                    num_comments=0,
                    created_utc=1_700_000_000.0,
                    author=None,
                )
            ]
        }
    )
    _wire(monkeypatch, session_cm, client)

    assert reddit.RedditConnector(session_factory=session_cm).fetch(subreddits=["stocks"]) == 1
    with session_cm() as session:
        post = session.scalar(select(SocialPost))
        assert post.author_hash is None
        assert post.body is None  # empty selftext -> NULL, not ""


def test_reddit_missing_credentials_raises():
    connector = reddit.RedditConnector(
        SimpleNamespace(reddit_client_id="", reddit_client_secret="")
    )
    with pytest.raises(RuntimeError, match="REDDIT_CLIENT_ID"):
        connector.fetch(subreddits=["stocks"])
