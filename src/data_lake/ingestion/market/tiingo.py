"""Tiingo end-of-day connector — prices for tickers Yahoo has dropped.

Yahoo forgets delisted tickers; Tiingo's free tier keeps ~7,400 dead NYSE/NASDAQ ones,
bankruptcies under their ``Q`` ticker (``BBBYQ``, ``SIVBQ``). That is what this connector is
for: backfilling the members a point-in-time index universe needs and Yahoo cannot price.

Free tier (pricing page): 500 unique symbols/month, 50 requests/hour, 1,000/day, internal
and personal use only. :class:`TiingoUsage` keeps a ledger that refuses requests *before*
the provider does, with defaults deliberately under those limits.

Auth is the ``Authorization: Token <key>`` header — never a query parameter, so the key stays
out of URLs, logs and every error message raised here.

Docs: https://www.tiingo.com/documentation/end-of-day
"""

import csv
import io
import json
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.base import Connector, SessionFactory
from data_lake.ingestion.market.bars import upsert_daily_bars
from data_lake.ingestion.market.yahoo_common import daily_ts
from data_lake.settings import LakeSettings

__all__ = [
    "PRICES_URL",
    "SUPPORTED_TICKERS_URL",
    "TiingoBudgetExhausted",
    "TiingoConnector",
    "TiingoListing",
    "TiingoProviderError",
    "TiingoUsage",
    "fetch_supported_tickers",
    "find_instrument",
    "instrument_key",
    "parse_supported_tickers",
]

PRICES_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
#: Public, no key needed: one CSV of every ticker Tiingo prices, dead ones included.
SUPPORTED_TICKERS_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"

#: Instrument exchange when the caller does not pass a listing's exchange.
DEFAULT_EXCHANGE = "TIINGO"
#: Start of a first fetch with no ``date_from``: Tiingo returns only the latest bar when
#: ``startDate`` is omitted, so a first fetch always names one.
EARLIEST_DATE = date(1962, 1, 1)


class TiingoProviderError(RuntimeError):
    """Tiingo rejected the request (auth, server error). Never carries the API key.

    ``status`` is the HTTP status when there was one, so a caller can tell a refused key
    (401/403: stop calling) from one ticker's server error (carry on).
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TiingoBudgetExhausted(RuntimeError):
    """The local usage ledger, or Tiingo itself (HTTP 429), says the quota is spent."""


@dataclass(frozen=True)
class TiingoListing:
    ticker: str
    exchange: str
    asset_type: str
    currency: str
    start: date
    end: date | None


def _optional_date(value: str) -> date | None:
    value = value.strip()
    return date.fromisoformat(value[:10]) if value else None


def parse_supported_tickers(zip_bytes: bytes) -> dict[str, list[TiingoListing]]:
    """Index ``supported_tickers.zip`` by upper-case ticker; rows without a startDate dropped.

    A ticker reused by a later company appears as several rows — kept, so a caller can tell
    an unambiguous ticker from a reused one.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not names:
            raise ValueError("supported_tickers.zip holds no CSV")
        text = archive.read(names[0]).decode("utf-8-sig")

    listings: dict[str, list[TiingoListing]] = {}
    for row in csv.DictReader(io.StringIO(text)):
        start = _optional_date(row.get("startDate") or "")
        ticker = (row.get("ticker") or "").strip().upper()
        if start is None or not ticker:
            continue
        listings.setdefault(ticker, []).append(
            TiingoListing(
                ticker=ticker,
                exchange=(row.get("exchange") or "").strip(),
                asset_type=(row.get("assetType") or "").strip(),
                currency=(row.get("priceCurrency") or "").strip(),
                start=start,
                end=_optional_date(row.get("endDate") or ""),
            )
        )
    return listings


def _http_get_bytes(url: str) -> bytes:
    """One GET. Isolated so tests can stub it without network."""
    response = httpx.get(url, timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.content


def fetch_supported_tickers() -> bytes:
    """Download ``supported_tickers.zip`` (no key, no quota)."""
    return _http_get_bytes(SUPPORTED_TICKERS_URL)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class TiingoUsage:
    """Request ledger that refuses a call before Tiingo's free-tier quota would.

    Monthly unique symbols reset with the calendar month; hourly and daily request counts
    are *rolling* windows, which is stricter than a calendar bucket (45 requests at 10:59
    plus 45 at 11:00 would be 90 inside one hour). Persisted as JSON when ``path`` is set, so
    separate processes on one machine share the budget; in memory otherwise.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        monthly_symbols: int = 450,
        hourly_requests: int = 45,
        daily_requests: int = 900,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.path = path
        self.monthly_symbols = monthly_symbols
        self.hourly_requests = hourly_requests
        self.daily_requests = daily_requests
        self._clock = clock
        self._month = ""
        self._symbols: set[str] = set()
        self._requests: list[datetime] = []
        if path is not None and path.exists():
            self._load(path)

    def _load(self, path: Path) -> None:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            self._month = str(state["month"])
            self._symbols = {str(symbol) for symbol in state["symbols"]}
            self._requests = [datetime.fromisoformat(ts) for ts in state["requests"]]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Resetting would silently hand back a spent budget; make the operator decide.
            raise ValueError(f"unreadable Tiingo usage ledger {path}: {exc}") from exc

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "month": self._month,
            "symbols": sorted(self._symbols),
            "requests": [ts.isoformat() for ts in self._requests],
        }
        self.path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _roll(self, now: datetime) -> None:
        month = now.strftime("%Y-%m")
        if month != self._month:
            self._month = month
            self._symbols = set()
        cutoff = now - timedelta(days=1)
        self._requests = [ts for ts in self._requests if ts > cutoff]

    def check(self, symbol: str) -> None:
        """Raise :class:`TiingoBudgetExhausted` if requesting ``symbol`` now would overrun."""
        now = self._clock()
        self._roll(now)
        symbol = symbol.upper()
        if symbol not in self._symbols and len(self._symbols) >= self.monthly_symbols:
            raise TiingoBudgetExhausted(
                f"Tiingo monthly symbol budget spent ({self.monthly_symbols} in {self._month})"
            )
        hour_ago = now - timedelta(hours=1)
        if sum(1 for ts in self._requests if ts > hour_ago) >= self.hourly_requests:
            raise TiingoBudgetExhausted(
                f"Tiingo hourly request budget spent ({self.hourly_requests})"
            )
        if len(self._requests) >= self.daily_requests:
            raise TiingoBudgetExhausted(
                f"Tiingo daily request budget spent ({self.daily_requests})"
            )

    def record(self, symbol: str) -> None:
        """Count one request for ``symbol`` (and the symbol against the monthly budget)."""
        now = self._clock()
        self._roll(now)
        self._symbols.add(symbol.upper())
        self._requests.append(now)
        self._save()


def _get_prices(ticker: str, params: dict[str, str], token: str) -> httpx.Response:
    """One prices GET. Isolated so tests can stub it without network."""
    return httpx.get(
        PRICES_URL.format(ticker=ticker),
        params=params,
        headers={"Authorization": f"Token {token}", "Content-Type": "application/json"},
        timeout=30,
    )


def _float_or(value: Any, fallback: float) -> float:
    return fallback if value is None else float(value)


def _bar_values(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Tiingo's split/dividend-adjusted fields to bar dicts; rows lacking adjClose drop."""
    values: list[dict[str, Any]] = []
    for row in rows:
        if row.get("adjClose") is None:
            continue
        close = float(row["adjClose"])
        volume = row.get("adjVolume")
        values.append(
            {
                "ts": daily_ts(date.fromisoformat(str(row["date"])[:10])),
                "open": _float_or(row.get("adjOpen"), close),
                "high": _float_or(row.get("adjHigh"), close),
                "low": _float_or(row.get("adjLow"), close),
                "close": close,
                "volume": None if volume is None else float(volume),
            }
        )
    return values


def instrument_key(ticker: str, exchange: str = "") -> tuple[str, str, str]:
    """The (symbol, exchange, currency) a Tiingo series is stored under.

    Keyed by the listing's exchange rather than Yahoo's ``SMART``, so a dead ticker's history
    never lands on the instrument of a later company that reused the ticker.
    """
    return ticker.strip().upper(), exchange.strip().upper() or DEFAULT_EXCHANGE, "USD"


def find_instrument(session: Session, ticker: str, exchange: str = "") -> Instrument | None:
    """The instrument a Tiingo series for ``ticker`` on ``exchange`` is stored under, if any."""
    symbol, exchange, currency = instrument_key(ticker, exchange)
    return session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
        )
    )


class TiingoConnector(Connector):
    name = "tiingo"

    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
        usage: TiingoUsage | None = None,
    ) -> None:
        super().__init__(settings=settings, session_factory=session_factory)
        self.usage = usage if usage is not None else TiingoUsage()

    def fetch(
        self,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        exchange: str = "",
        **kwargs,
    ) -> int:
        """Upsert adjusted daily bars for one Tiingo ticker; returns bars written.

        With no ``date_from`` the fetch resumes the day after the last stored Tiingo bar, or
        starts at :data:`EARLIEST_DATE`. An unknown ticker (HTTP 404) returns 0 and creates
        no instrument.
        """
        token = self.settings.tiingo_api_key
        if not token:
            raise RuntimeError("TIINGO_API_KEY is not set")
        ticker = symbol.strip().upper()
        if not ticker:
            raise ValueError("symbol is required")
        start = date.fromisoformat(date_from) if date_from else None
        if start is None:
            with self.session() as session:
                instrument = find_instrument(session, ticker, exchange)
                latest = (
                    session.scalar(
                        select(func.max(PriceBar.ts)).where(
                            PriceBar.instrument_id == instrument.id,
                            PriceBar.source == self.name,
                            PriceBar.what_to_show == "ADJUSTED_LAST",
                            PriceBar.bar_size == "1 day",
                        )
                    )
                    if instrument
                    else None
                )
            start = latest.date() + timedelta(days=1) if latest else EARLIEST_DATE
        params = {"startDate": start.isoformat(), "format": "json"}
        if date_to:
            if start > date.fromisoformat(date_to):
                return 0
            params["endDate"] = date_to

        self.usage.check(ticker)
        try:
            response = _get_prices(ticker, params, token)
        except httpx.HTTPError as exc:
            raise TiingoProviderError(
                f"Tiingo request for {ticker} failed: {type(exc).__name__}"
            ) from None
        self.usage.record(ticker)

        status = response.status_code
        if status == 404:
            return 0
        if status == 429:
            raise TiingoBudgetExhausted(f"Tiingo answered HTTP 429 for {ticker}")
        if status in (401, 403):
            raise TiingoProviderError(
                f"Tiingo refused the request for {ticker} with HTTP {status}. "
                "Check TIINGO_API_KEY (omitted from this error).",
                status=status,
            )
        if status >= 400:
            raise TiingoProviderError(
                f"Tiingo request for {ticker} failed with HTTP {status}", status=status
            )

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, list):
            raise TiingoProviderError(f"unexpected Tiingo response shape for {ticker}")
        values = _bar_values(payload)
        if not values:
            return 0

        with self.session() as session:
            instrument = find_instrument(session, ticker, exchange)
            if instrument is None:
                key_symbol, key_exchange, currency = instrument_key(ticker, exchange)
                instrument = Instrument(symbol=key_symbol, exchange=key_exchange, currency=currency)
                session.add(instrument)
                session.flush()
            return upsert_daily_bars(session, instrument.id, values, source=self.name)
