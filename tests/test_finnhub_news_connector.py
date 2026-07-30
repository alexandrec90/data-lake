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
from data_lake.ingestion.news import finnhub_news as fh


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
        request = fh.httpx.Request("GET", "https://finnhub.io/sanitized")
        response = fh.httpx.Response(self.status_code, request=request)
        raise fh.httpx.HTTPStatusError("sanitized", request=request, response=response)


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


def _item(id_: int, headline: str, dt: int = 1_700_000_000) -> dict:
    return {
        "id": id_,
        "datetime": dt,
        "headline": headline,
        "summary": "summary text",
        "url": f"https://news/{id_}",
        "source": "Reuters",
        "category": "company",
        "related": "AAPL",
        "image": "https://img/big.jpg",
    }


def test_finnhub_fetch_upserts_articles(monkeypatch):
    session_cm = _make_session_scope()
    payload = [_item(1, "AAPL beats"), _item(2, "AAPL guidance")]

    def fake_get(url, params, headers, timeout):
        assert url == f"{fh.BASE_URL}/company-news"
        assert params["symbol"] == "AAPL"
        assert params["from"] and params["to"]
        assert headers["X-Finnhub-Token"] == "test-key"
        assert timeout == 30
        return FakeResponse(payload)

    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    monkeypatch.setattr(fh.httpx, "get", fake_get)

    count = fh.FinnhubNewsConnector(session_factory=session_cm).fetch(symbol="aapl")

    assert count == 2
    with session_cm() as session:
        articles = session.scalars(select(NewsArticle).order_by(NewsArticle.external_id)).all()
        assert len(articles) == 2
        first = articles[0]
        assert first.source == "finnhub"
        assert first.external_id == "1"
        assert first.title == "AAPL beats"
        assert first.summary == "summary text"
        assert first.symbols == ["AAPL"]
        # SQLite drops tzinfo on readback (Postgres keeps it); compare the naive instant.
        assert first.published_at.replace(tzinfo=UTC) == datetime.fromtimestamp(
            1_700_000_000, tz=UTC
        )
        # raw is trimmed — image URL dropped
        assert set(first.raw) == {"source", "category", "related"}
        assert "image" not in first.raw


def test_finnhub_fetch_is_idempotent_on_reingest(monkeypatch):
    session_cm = _make_session_scope()
    payloads = [[_item(1, "old headline")], [_item(1, "updated headline")]]

    def fake_get(url, params, headers, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    monkeypatch.setattr(fh.httpx, "get", fake_get)

    assert fh.FinnhubNewsConnector(session_factory=session_cm).fetch(symbol="AAPL") == 1
    assert fh.FinnhubNewsConnector(session_factory=session_cm).fetch(symbol="AAPL") == 1

    with session_cm() as session:
        articles = session.scalars(select(NewsArticle)).all()
        assert len(articles) == 1  # upsert, not duplicate
        assert articles[0].title == "updated headline"


def test_finnhub_fetch_merges_symbol_tags(monkeypatch):
    """Same article surfacing under a second ticker keeps both tags, unique."""
    session_cm = _make_session_scope()
    payloads = [[_item(1, "shared")], [_item(1, "shared")]]

    def fake_get(url, params, headers, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    monkeypatch.setattr(fh.httpx, "get", fake_get)

    fh.FinnhubNewsConnector(session_factory=session_cm).fetch(symbol="AAPL")
    fh.FinnhubNewsConnector(session_factory=session_cm).fetch(symbol="MSFT")

    with session_cm() as session:
        article = session.scalar(select(NewsArticle))
        assert article.symbols == ["AAPL", "MSFT"]


def test_finnhub_uses_explicit_window(monkeypatch):
    session_cm = _make_session_scope()
    seen: dict[str, str] = {}

    def fake_get(url, params, headers, timeout):
        seen.update(params)
        return FakeResponse([])

    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    monkeypatch.setattr(fh.httpx, "get", fake_get)

    assert (
        fh.FinnhubNewsConnector(session_factory=session_cm).fetch(
            symbol="AAPL", date_from="2024-01-01", date_to="2024-01-31"
        )
        == 0
    )
    assert seen["from"] == "2024-01-01"
    assert seen["to"] == "2024-01-31"


def test_finnhub_bad_date_raises(monkeypatch):
    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    with pytest.raises(ValueError):
        fh.FinnhubNewsConnector().fetch(symbol="AAPL", date_from="not-a-date")


def test_finnhub_missing_key_raises():
    connector = fh.FinnhubNewsConnector(SimpleNamespace(finnhub_key=""))
    with pytest.raises(RuntimeError, match="FINNHUB_KEY"):
        connector.fetch(symbol="AAPL")


def test_finnhub_sanitizes_http_errors(monkeypatch):
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        return FakeHttpErrorResponse({"error": "nope"}, status_code=429)

    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    monkeypatch.setattr(fh.httpx, "get", fake_get)

    with pytest.raises(fh.FinnhubProviderError) as exc_info:
        fh.FinnhubNewsConnector(session_factory=session_cm).fetch(symbol="AAPL")
    assert "HTTP 429" in str(exc_info.value)
    assert "API key omitted" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
