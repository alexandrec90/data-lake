from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from _lake_env import SETTINGS
from data_lake.db.models import Base, NewsArticle
from data_lake.ingestion.base import stable_hash
from data_lake.ingestion.news import newsapi as na


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeHttpErrorResponse(FakeResponse):
    def raise_for_status(self) -> None:
        request = na.httpx.Request("GET", "https://newsapi.org/sanitized")
        response = na.httpx.Response(self.status_code, request=request)
        raise na.httpx.HTTPStatusError("sanitized", request=request, response=response)


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


def _article(url: str, title: str, published: str = "2026-07-15T12:00:00Z") -> dict:
    return {
        "source": {"id": None, "name": "Example Wire"},
        "author": "Jane Byline",
        "title": title,
        "description": "description text",
        "url": url,
        "urlToImage": "https://img/big.jpg",
        "publishedAt": published,
        "content": "full body we must not store",
    }


def _payload(*articles: dict) -> dict:
    return {"status": "ok", "totalResults": len(articles), "articles": list(articles)}


def test_newsapi_fetch_upserts_articles(monkeypatch):
    session_cm = _make_session_scope()
    payload = _payload(_article("https://news/a", "Nvidia beats"), _article("https://news/b", "x"))

    def fake_get(url, params, headers, timeout):
        assert url == f"{na.BASE_URL}/everything"
        assert params["q"] == "Nvidia"
        assert params["pageSize"] == na.PAGE_SIZE
        assert headers["X-Api-Key"] == "test-key"
        assert timeout == 30
        return FakeResponse(payload)

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    count = na.NewsApiConnector(session_factory=session_cm).fetch(query="Nvidia", symbol="nvda")

    assert count == 2
    with session_cm() as session:
        articles = session.scalars(select(NewsArticle).order_by(NewsArticle.title)).all()
        assert len(articles) == 2
        first = articles[0]
        assert first.source == "newsapi"
        assert first.external_id == stable_hash("https://news/a")
        assert first.title == "Nvidia beats"
        assert first.summary == "description text"
        assert first.symbols == ["NVDA"]
        # SQLite drops tzinfo on readback (Postgres keeps it); compare the naive instant.
        assert first.published_at.replace(tzinfo=UTC) == datetime(2026, 7, 15, 12, tzinfo=UTC)
        # raw is trimmed — provider name only; author/image/content never stored
        assert first.raw == {"source": "Example Wire"}


def test_newsapi_fetch_is_idempotent_on_reingest(monkeypatch):
    session_cm = _make_session_scope()
    payloads = [
        _payload(_article("https://news/a", "old headline")),
        _payload(_article("https://news/a", "updated headline")),
    ]

    def fake_get(url, params, headers, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    assert na.NewsApiConnector(session_factory=session_cm).fetch(query="Nvidia") == 1
    assert na.NewsApiConnector(session_factory=session_cm).fetch(query="Nvidia") == 1

    with session_cm() as session:
        articles = session.scalars(select(NewsArticle)).all()
        assert len(articles) == 1  # upsert on stable_hash(url), not duplicate
        assert articles[0].title == "updated headline"


def test_newsapi_merges_symbol_tags(monkeypatch):
    """Same article surfacing under two query/symbol runs keeps both tags, unique."""
    session_cm = _make_session_scope()
    payloads = [
        _payload(_article("https://news/a", "shared")),
        _payload(_article("https://news/a", "shared")),
    ]

    def fake_get(url, params, headers, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    na.NewsApiConnector(session_factory=session_cm).fetch(query="Nvidia", symbol="NVDA")
    na.NewsApiConnector(session_factory=session_cm).fetch(query="AMD", symbol="AMD")

    with session_cm() as session:
        article = session.scalar(select(NewsArticle))
        assert article.symbols == ["NVDA", "AMD"]


def test_newsapi_skips_items_without_url(monkeypatch):
    session_cm = _make_session_scope()
    broken = _article("https://news/a", "ok")
    broken_no_url = dict(_article("", "no url"), url=None)
    broken_no_date = dict(_article("https://news/c", "no date"), publishedAt=None)

    def fake_get(url, params, headers, timeout):
        return FakeResponse(_payload(broken, broken_no_url, broken_no_date))

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    assert na.NewsApiConnector(session_factory=session_cm).fetch(query="Nvidia") == 1


def test_newsapi_passes_explicit_window(monkeypatch):
    session_cm = _make_session_scope()
    seen: dict[str, object] = {}

    def fake_get(url, params, headers, timeout):
        seen.update(params)
        return FakeResponse(_payload())

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    assert (
        na.NewsApiConnector(session_factory=session_cm).fetch(
            query="Nvidia", date_from="2026-06-20", date_to="2026-07-15"
        )
        == 0
    )
    assert seen["from"] == "2026-06-20"
    assert seen["to"] == "2026-07-15"


def test_newsapi_bad_date_raises(monkeypatch):
    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    with pytest.raises(ValueError):
        na.NewsApiConnector().fetch(query="Nvidia", date_from="not-a-date")


def test_newsapi_empty_query_raises(monkeypatch):
    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    with pytest.raises(ValueError, match="query"):
        na.NewsApiConnector().fetch(query="   ")


def test_newsapi_missing_key_raises():
    connector = na.NewsApiConnector(SimpleNamespace(newsapi_key=""))
    with pytest.raises(RuntimeError, match="NEWSAPI_KEY"):
        connector.fetch(query="Nvidia")


def test_newsapi_sanitizes_http_errors(monkeypatch):
    def fake_get(url, params, headers, timeout):
        return FakeHttpErrorResponse({"status": "error"}, status_code=429)

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    with pytest.raises(na.NewsApiProviderError) as exc_info:
        na.NewsApiConnector().fetch(query="Nvidia")
    assert "HTTP 429" in str(exc_info.value)
    assert "API key omitted" in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_fresh_tagged_symbols_only_returns_recently_fetched(monkeypatch):
    from datetime import timedelta

    session_cm = _make_session_scope()
    now = datetime.now(UTC)

    def _row(external_id: str, symbols: list[str], fetched_at, source: str = "newsapi"):
        return NewsArticle(
            source=source,
            external_id=external_id,
            published_at=now,
            title="t",
            url="https://news/x",
            summary=None,
            symbols=symbols,
            sentiment=None,
            raw={},
            fetched_at=fetched_at,
        )

    with session_cm() as session:
        session.add(_row("1", ["NVDA"], now - timedelta(hours=1)))  # fresh
        session.add(_row("2", ["AAPL"], now - timedelta(hours=30)))  # stale
        session.add(_row("3", ["MSFT"], now, source="finnhub"))  # other source — ignored
        session.add(_row("4", None, now))  # untagged — ignored

    assert na.fresh_tagged_symbols(now - timedelta(hours=12), session_factory=session_cm) == {
        "NVDA"
    }


def test_newsapi_unexpected_shape_raises(monkeypatch):
    def fake_get(url, params, headers, timeout):
        return FakeResponse({"status": "ok", "articles": "not-a-list"})

    monkeypatch.setattr(SETTINGS, "newsapi_key", "test-key")
    monkeypatch.setattr(na.httpx, "get", fake_get)

    with pytest.raises(na.NewsApiProviderError, match="response shape"):
        na.NewsApiConnector().fetch(query="Nvidia")
