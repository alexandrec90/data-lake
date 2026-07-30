"""NewsAPI (newsapi.org) connector — headline search by keyword/company name.

Free (Developer) tier: ~100 requests/day, non-commercial, articles delayed 24 h, and only
the first 100 results of a search are accessible — so one request at pageSize=100 per fetch
is deliberately all this connector does. Docs: https://newsapi.org/docs/endpoints/everything

Articles are keyed by ``stable_hash(url)`` (NewsAPI has no article id). NewsAPI does not tag
tickers, so pass ``symbol=`` to tag stored rows for downstream signals; the ``raw`` column
keeps only the provider name — never the full payload (free disk is tight on this box).
"""

from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select

from data_lake.db.models import NewsArticle
from data_lake.ingestion.base import (
    Connector,
    SessionFactory,
    resolve_session_factory,
    stable_hash,
)

BASE_URL = "https://newsapi.org/v2"

#: Free-tier ceiling per request; also the total the free tier can see per search.
PAGE_SIZE = 100


class NewsApiProviderError(RuntimeError):
    """NewsAPI rejected the request or returned a provider-side failure.

    ``status_code`` lets batch callers tell a per-query failure from a key/quota problem
    (401/426/429) that would make every remaining request fail too.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


#: HTTP statuses that doom every subsequent call this run (bad key, plan limit, daily quota).
FATAL_STATUSES = frozenset({401, 426, 429})


def _published_at(value: str) -> datetime:
    # NewsAPI returns ISO-8601 with a trailing "Z" (UTC).
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _trimmed_raw(item: dict[str, Any]) -> dict[str, Any]:
    """Small fixed field set — never the whole item (drops image URLs, content, author)."""
    source = item.get("source") or {}
    return {"source": source.get("name") if isinstance(source, dict) else None}


def _merge_symbol(symbols: list | None, symbol: str) -> list:
    existing = list(symbols) if symbols else []
    if symbol and symbol not in existing:
        existing.append(symbol)
    return existing


class NewsApiConnector(Connector):
    name = "newsapi"

    def fetch(
        self,
        query: str = "",
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        **kwargs,
    ) -> int:
        settings = self.settings
        if not settings.newsapi_key:
            raise RuntimeError("NEWSAPI_KEY is not set (see .env.example)")
        if not query.strip():
            raise ValueError("query is required")
        tag_symbol = symbol.strip().upper()

        params: dict[str, Any] = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": PAGE_SIZE,
        }
        # Validate loudly rather than letting NewsAPI return an opaque 4xx.
        if date_from:
            date.fromisoformat(date_from)
            params["from"] = date_from
        if date_to:
            date.fromisoformat(date_to)
            params["to"] = date_to

        response = _get_everything(params=params, api_key=settings.newsapi_key)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise NewsApiProviderError(
                f"NewsAPI request failed with HTTP {status}. Check NEWSAPI_KEY, plan limits "
                "(free tier: 100 req/day, first 100 results only). API key omitted from error.",
                status_code=status,
            ) from None

        payload = response.json()
        articles = payload.get("articles") if isinstance(payload, dict) else None
        if not isinstance(articles, list):
            raise NewsApiProviderError("unexpected NewsAPI /everything response shape")

        fetched_at = datetime.now(UTC)
        count = 0
        with self.session() as session:
            for item in articles:
                if not isinstance(item, dict) or not item.get("url") or not item.get("publishedAt"):
                    continue
                external_id = stable_hash(str(item["url"]))
                existing = session.scalar(
                    select(NewsArticle).where(
                        NewsArticle.source == self.name,
                        NewsArticle.external_id == external_id,
                    )
                )
                if existing:
                    existing.title = item.get("title") or existing.title
                    existing.url = item.get("url")
                    existing.summary = item.get("description")
                    existing.symbols = _merge_symbol(existing.symbols, tag_symbol)
                    existing.raw = _trimmed_raw(item)
                    existing.fetched_at = fetched_at
                else:
                    session.add(
                        NewsArticle(
                            source=self.name,
                            external_id=external_id,
                            published_at=_published_at(str(item["publishedAt"])),
                            title=item.get("title") or "",
                            url=item.get("url"),
                            summary=item.get("description"),
                            symbols=[tag_symbol] if tag_symbol else [],
                            sentiment=None,  # scored later in signals/
                            raw=_trimmed_raw(item),
                            fetched_at=fetched_at,
                        )
                    )
                count += 1
        return count


def fresh_tagged_symbols(
    cutoff: datetime, session_factory: SessionFactory | None = None
) -> set[str]:
    """Symbols that already have a newsapi article fetched after ``cutoff``.

    One query for a whole batch: batch pollers skip these symbols instead of re-spending a
    request on data that was just pulled. The window is small (free tier stores ≤ ~100
    articles/day), so filtering the JSON symbol tags in Python stays cheap and portable
    across Postgres and the SQLite test harness. A query that returned 0 articles stores
    nothing and therefore never looks fresh — the batch request budget bounds the retry cost.
    """
    fresh: set[str] = set()
    with resolve_session_factory(session_factory)() as session:
        rows = session.execute(
            select(NewsArticle.symbols).where(
                NewsArticle.source == NewsApiConnector.name,
                NewsArticle.fetched_at >= cutoff,
            )
        )
        for (symbols,) in rows:
            fresh.update(symbols or [])
    return fresh


def _get_everything(params: dict[str, Any], api_key: str) -> httpx.Response:
    return httpx.get(
        f"{BASE_URL}/everything",
        params=params,
        headers={"X-Api-Key": api_key},
        timeout=30,
    )
