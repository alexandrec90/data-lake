"""Finnhub company-news connector.

Free tier: ~60 calls/min, company news keyed by ticker (better than NewsAPI for finance since
articles arrive already tagged to a symbol). Docs: https://finnhub.io/docs/api/company-news
Finnhub also exposes a news-sentiment endpoint worth evaluating later.

Disk note: the ``raw`` JSON column stores a *trimmed* dict, never the full article payload —
free disk is tight on this box. Re-fetching an overlapping window is idempotent (upsert on
(source, external_id)).
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from data_lake.db.models import NewsArticle
from data_lake.ingestion.base import Connector

BASE_URL = "https://finnhub.io/api/v1"

#: Default look-back when no explicit window is passed. Finnhub requires from/to.
DEFAULT_LOOKBACK_DAYS = 7


class FinnhubProviderError(RuntimeError):
    """Finnhub rejected the request or returned a provider-side failure."""


def _default_window() -> tuple[str, str]:
    today = datetime.now(UTC).date()
    start = today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return start.isoformat(), today.isoformat()


def _published_at(item: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(float(item["datetime"]), tz=UTC)


def _trimmed_raw(item: dict[str, Any]) -> dict[str, Any]:
    """Small fixed field set — never the whole item (drops image URLs etc.)."""
    return {
        "source": item.get("source"),
        "category": item.get("category"),
        "related": item.get("related"),
    }


class FinnhubNewsConnector(Connector):
    name = "finnhub"

    def fetch(self, symbol: str = "", date_from: str = "", date_to: str = "", **kwargs) -> int:
        settings = self.settings
        if not settings.finnhub_key:
            raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")
        finnhub_symbol = symbol.strip().upper()
        if not finnhub_symbol:
            raise ValueError("symbol is required")

        default_from, default_to = _default_window()
        window_from = date_from or default_from
        window_to = date_to or default_to
        # Validate the format loudly rather than letting Finnhub silently return [].
        date.fromisoformat(window_from)
        date.fromisoformat(window_to)

        response = _get_company_news(
            symbol=finnhub_symbol,
            date_from=window_from,
            date_to=window_to,
            api_key=settings.finnhub_key,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise FinnhubProviderError(
                f"Finnhub request failed with HTTP {status}. Check FINNHUB_KEY, plan access, "
                "and rate limits. API key omitted from error."
            ) from None

        items = response.json()
        if not isinstance(items, list):
            raise FinnhubProviderError("unexpected Finnhub company-news response shape")

        fetched_at = datetime.now(UTC)
        count = 0
        with self.session() as session:
            for item in items:
                if not isinstance(item, dict) or "id" not in item:
                    continue
                external_id = str(item["id"])
                existing = session.scalar(
                    select(NewsArticle).where(
                        NewsArticle.source == self.name,
                        NewsArticle.external_id == external_id,
                    )
                )
                if existing:
                    existing.title = item.get("headline") or existing.title
                    existing.url = item.get("url")
                    existing.summary = item.get("summary")
                    existing.symbols = _merge_symbol(existing.symbols, finnhub_symbol)
                    existing.raw = _trimmed_raw(item)
                    existing.fetched_at = fetched_at
                else:
                    session.add(
                        NewsArticle(
                            source=self.name,
                            external_id=external_id,
                            published_at=_published_at(item),
                            title=item.get("headline") or "",
                            url=item.get("url"),
                            summary=item.get("summary"),
                            symbols=[finnhub_symbol],
                            sentiment=None,  # scored later in signals/
                            raw=_trimmed_raw(item),
                            fetched_at=fetched_at,
                        )
                    )
                count += 1
        return count


def _merge_symbol(symbols: list | None, symbol: str) -> list:
    """Keep the article's symbol tags unique when the same article surfaces under two tickers."""
    existing = list(symbols) if symbols else []
    if symbol not in existing:
        existing.append(symbol)
    return existing


def _get_company_news(symbol: str, date_from: str, date_to: str, api_key: str) -> httpx.Response:
    return httpx.get(
        f"{BASE_URL}/company-news",
        params={"symbol": symbol, "from": date_from, "to": date_to},
        headers={"X-Finnhub-Token": api_key},
        timeout=30,
    )
