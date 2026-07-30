"""Google Trends connector via pytrends (UNOFFICIAL — expect breakage and throttling).

pytrends scrapes the Trends site; Google rate-limits aggressively (HTTP 429). Keep request
volume tiny (a handful of keyword batches per day), add long backoff, and treat this source as
best-effort garnish, not a load-bearing input.

The interest value is a 0-100 index **relative to the request window**, so points are only
comparable when the timeframe is held constant — this connector pins a single default
``timeframe`` and stores ``(keyword, geo, ts)``. Change the window and old/new points stop
being comparable.
"""

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import TrendPoint
from data_lake.ingestion.base import Connector

#: Keep points comparable across fetches — do not vary this per call.
DEFAULT_TIMEFRAME = "now 7-d"

#: pytrends is unofficial and rate-limited hard; space calls far apart. Module-level so any
#: future batching shares one budget. Tests set this to 0.
MIN_REQUEST_INTERVAL_SECONDS = 60.0
_last_request_monotonic: float | None = None


def throttle() -> None:
    """Sleep so consecutive Trends requests are at least the min interval apart."""
    global _last_request_monotonic
    now = time.monotonic()
    if _last_request_monotonic is not None:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
    _last_request_monotonic = time.monotonic()


def _trends_client() -> Any:
    """Build a pytrends client. Isolated so tests monkeypatch this and never import pytrends."""
    from pytrends.request import TrendReq

    # tz in minutes; 300 = UTC-5 (Montréal). hl = host language.
    return TrendReq(hl="en-US", tz=300)


class GoogleTrendsConnector(Connector):
    name = "google_trends"

    def fetch(
        self,
        keywords: list[str] | None = None,
        geo: str = "CA",
        timeframe: str = DEFAULT_TIMEFRAME,
        skip_if_newer_than_days: float = 0.0,
        **kwargs,
    ) -> int:
        keywords = [kw.strip() for kw in (keywords or []) if kw.strip()]
        if not keywords:
            raise ValueError("at least one keyword is required")
        # Trends caps a single payload at 5 keywords.
        batch = keywords[:5]

        # Freshness skip: each fetch re-pulls (and renormalizes) the whole window, so the only
        # savings available is not fetching at all. Weekly data can't produce a new bucket more
        # often than ~every 7 days; skipping fresh keywords makes reruns cost seconds and lets
        # a partially-failed batch resume (failed keywords stay stale and get retried).
        if skip_if_newer_than_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=skip_if_newer_than_days)
            with self.session() as session:
                batch = [kw for kw in batch if _is_stale(session, kw, geo, cutoff)]
            if not batch:
                return 0  # everything fresh — no request, no throttle wait

        throttle()
        pytrends = _trends_client()
        pytrends.build_payload(kw_list=batch, timeframe=timeframe, geo=geo)
        frame = pytrends.interest_over_time()

        if frame is None or frame.empty:
            return 0

        count = 0
        with self.session() as session:
            for index, row in frame.iterrows():
                if bool(row.get("isPartial", False)):
                    continue  # provisional bucket — skip so we don't store a moving value
                ts = _to_utc(index)
                for keyword in batch:
                    if keyword not in row:
                        continue
                    interest = float(row[keyword])
                    existing = session.scalar(
                        select(TrendPoint).where(
                            TrendPoint.keyword == keyword,
                            TrendPoint.geo == geo,
                            TrendPoint.ts == ts,
                        )
                    )
                    if existing:
                        existing.interest = interest
                    else:
                        session.add(TrendPoint(keyword=keyword, geo=geo, ts=ts, interest=interest))
                    count += 1
        return count


def read_mapping_file(path: str) -> list[tuple[str, str]]:
    """Parse ``TICKER,search term`` lines into (symbol, keyword) pairs.

    Blank lines and ``#`` comments are skipped; a malformed line raises loudly rather than
    silently dropping a keyword. The symbol side ties each Trends series back to the
    tickers.txt universe (trend_points stores only the search term).
    """
    pairs: list[tuple[str, str]] = []
    with open(path, encoding="utf-8-sig") as handle:  # -sig: tolerate a Notepad BOM
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            symbol, sep, keyword = stripped.partition(",")
            if not sep or not symbol.strip() or not keyword.strip():
                raise ValueError(
                    f"{path}:{lineno}: expected 'TICKER,search term', got {stripped!r}"
                )
            pairs.append((symbol.strip().upper(), keyword.strip()))
    return pairs


def _is_stale(session: Session, keyword: str, geo: str, cutoff: datetime) -> bool:
    """True when the stored series for (keyword, geo) is absent or older than the cutoff."""
    newest = session.scalar(
        select(func.max(TrendPoint.ts)).where(TrendPoint.keyword == keyword, TrendPoint.geo == geo)
    )
    if newest is None:
        return True
    if newest.tzinfo is None:  # SQLite drops tzinfo on readback (Postgres keeps it)
        newest = newest.replace(tzinfo=UTC)
    return newest <= cutoff


def _to_utc(index_value: Any) -> datetime:
    """pytrends indexes by naive/tz-aware timestamps; normalise to a UTC datetime."""
    dt = index_value.to_pydatetime() if hasattr(index_value, "to_pydatetime") else index_value
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
