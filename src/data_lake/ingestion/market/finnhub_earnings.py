"""Finnhub earnings-calendar connector -> ``earnings_events`` (source ``finnhub``).

Docs: https://finnhub.io/docs/api/earnings-calendar. ``GET /calendar/earnings?from=&to=``
returns ``{"earningsCalendar": [{symbol, date, hour, year, quarter, epsEstimate, epsActual,
revenueEstimate, revenueActual}]}`` for every US symbol at once. The free tier serves about a
month of history plus upcoming dates; how far ahead it goes, and whether it silently cuts a
long window short, is not documented — so the window is requested in spans of at most
``MAX_SPAN_DAYS``, each well inside what is known to be served.

- ``report_ts`` is the calendar date at 00:00 UTC; ``hour`` (``bmo`` before the open, ``amc``
  after the close, ``dmh`` during market hours) says when in the day.
- Rows are stored **only for symbols already in** ``instruments`` (matched on ``symbol`` with
  currency USD). The calendar lists thousands of companies; this connector never creates an
  instrument, and logs how many rows it skipped.
- An upcoming report that disappears from the window was re-dated, and is deleted at its old
  date (``earnings_common``). Only after a non-empty response, and only inside the window.

The whole window is fetched before anything is written, and written in one transaction, so a
failed span commits nothing.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument
from data_lake.ingestion.base import Connector
from data_lake.ingestion.market.earnings_common import (
    delete_vanished_future_earnings,
    upsert_earnings_event,
)
from data_lake.ingestion.market.yahoo_common import daily_ts
from data_lake.ingestion.news.finnhub_news import FinnhubProviderError

__all__ = ["FinnhubEarningsCalendarConnector", "FinnhubProviderError"]

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
SOURCE = "finnhub"

#: Longest window one request asks for. A month is what the free tier documents serving.
MAX_SPAN_DAYS = 30

_HOURS = frozenset({"bmo", "amc", "dmh"})


def spans(first: date, last: date, max_days: int = MAX_SPAN_DAYS) -> list[tuple[date, date]]:
    """``[first, last]`` cut into consecutive inclusive spans of at most ``max_days`` days."""
    if max_days < 1:
        raise ValueError("max_days must be >= 1")
    result = []
    start = first
    while start <= last:
        end = min(start + timedelta(days=max_days - 1), last)
        result.append((start, end))
        start = end + timedelta(days=1)
    return result


def _number(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def row_fields(item: dict[str, Any]) -> tuple[str, date, dict[str, Any]] | None:
    """``(symbol, date, earnings_events fields)`` for one calendar row, or None if unusable."""
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    try:
        day = date.fromisoformat(str(item.get("date")))
    except ValueError:
        return None
    hour = str(item.get("hour") or "").strip().lower()
    return (
        symbol.strip().upper(),
        day,
        {
            "eps_estimate": _number(item.get("epsEstimate")),
            "eps_actual": _number(item.get("epsActual")),
            "revenue_estimate": _number(item.get("revenueEstimate")),
            "revenue_actual": _number(item.get("revenueActual")),
            "hour": hour if hour in _HOURS else None,
        },
    )


class FinnhubEarningsCalendarConnector(Connector):
    name = "finnhub"

    def fetch(self, days_back: int = 7, days_ahead: int = 90, **kwargs) -> int:
        """Upsert calendar rows dated from ``days_back`` ago to ``days_ahead`` from today,
        for instruments already tracked. Returns rows upserted."""
        settings = self.settings
        if not settings.finnhub_key:
            raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")
        if days_back < 0 or days_ahead < 0:
            raise ValueError("days_back and days_ahead must be >= 0")
        now = datetime.now(UTC)
        first = now.date() - timedelta(days=days_back)
        last = now.date() + timedelta(days=days_ahead)

        responses = [
            (span_from, span_to, _get_calendar(span_from, span_to, settings.finnhub_key))
            for span_from, span_to in spans(first, last)
        ]
        rows = [
            parsed
            for _, _, span_items in responses
            for item in span_items
            if isinstance(item, dict)
            and (parsed := row_fields(item)) is not None
            and first <= parsed[1] <= last
        ]

        with self.session() as session:
            seen, skipped = _upsert_rows(session, rows, tracked_instruments(session), now)
            for span_from, span_to, span_items in responses:
                if not span_items:
                    continue  # an empty span is as likely an outage as a quiet month
                delete_vanished_future_earnings(
                    session,
                    source=SOURCE,
                    keep=seen,
                    after=max(now, daily_ts(span_from) - timedelta(microseconds=1)),
                    until=daily_ts(span_to + timedelta(days=1)),
                )
        logger.info(
            "finnhub earnings: %d rows upserted, %d skipped (symbol not in instruments)",
            len(seen),
            skipped,
        )
        return len(seen)


def tracked_instruments(session: Session) -> dict[str, list[int]]:
    """Upper-cased symbol -> ids of the USD instruments carrying it."""
    instruments: dict[str, list[int]] = {}
    for instrument_id, symbol in session.execute(
        select(Instrument.id, Instrument.symbol).where(Instrument.currency == "USD")
    ):
        instruments.setdefault(symbol.upper(), []).append(instrument_id)
    return instruments


def _upsert_rows(
    session: Session,
    rows: list[tuple[str, date, dict[str, Any]]],
    instruments: dict[str, list[int]],
    now: datetime,
) -> tuple[set[tuple[int, datetime]], int]:
    """Upsert parsed rows for tracked symbols; return (keys written, rows skipped)."""
    seen: set[tuple[int, datetime]] = set()
    skipped = 0
    for symbol, day, fields in rows:
        instrument_ids = instruments.get(symbol)
        if not instrument_ids:
            skipped += 1
            continue
        report_ts = daily_ts(day)
        for instrument_id in instrument_ids:
            if (instrument_id, report_ts) not in seen:  # a report listed twice: first row wins
                upsert_earnings_event(session, instrument_id, report_ts, SOURCE, fields, now)
                seen.add((instrument_id, report_ts))
    return seen, skipped


def _get_calendar(span_from: date, span_to: date, api_key: str) -> list[Any]:
    try:
        response = httpx.get(
            f"{BASE_URL}/calendar/earnings",
            params={"from": span_from.isoformat(), "to": span_to.isoformat()},
            headers={"X-Finnhub-Token": api_key},
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FinnhubProviderError(
            f"Finnhub earnings calendar request failed with HTTP {exc.response.status_code}. "
            "Check FINNHUB_KEY, plan access, and rate limits. API key omitted from error."
        ) from None
    except httpx.HTTPError as exc:
        raise FinnhubProviderError(
            f"Finnhub earnings calendar request failed ({type(exc).__name__}). "
            "API key omitted from error."
        ) from None
    payload = response.json()
    items = payload.get("earningsCalendar") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise FinnhubProviderError("unexpected Finnhub earnings-calendar response shape")
    return items
