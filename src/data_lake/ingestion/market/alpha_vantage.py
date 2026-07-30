"""Alpha Vantage connector — daily(+adjusted) OHLCV. Free tier is tiny (~25 req/day):
use it for slow-moving backfills only. Docs: https://www.alphavantage.co/documentation/

``TIME_SERIES_DAILY_ADJUSTED`` is a premium endpoint on current free keys, so the connector
tries it first (bars stored as ``what_to_show="ADJUSTED_LAST"``, OHL back-adjusted by the
adjusted-close ratio) and falls back to unadjusted ``TIME_SERIES_DAILY``
(``what_to_show="TRADES"``) when Alpha Vantage refuses the premium call.

Alpha Vantage reports failures as HTTP 200 with an ``Error Message`` / ``Note`` /
``Information`` JSON body — those raise ``AlphaVantageProviderError`` here, and quota
exhaustion specifically raises ``AlphaVantageQuotaError`` so batch runs abort instead of
burning the rest of the day's ~25 requests on calls that can only fail.

NB: TSX symbols use the ``SHOP.TRT`` style on Alpha Vantage; those map to the same
(symbol, TSX, CAD) instrument key the other connectors use.
"""

import logging
import time as time_mod
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.base import Connector, SessionFactory, resolve_session_factory

logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"
SERIES_KEY = "Time Series (Daily)"
BAR_SIZE = "1 day"

#: Once Alpha Vantage refuses the adjusted endpoint as premium, every later call this
#: process would be refused too — remember and go straight to the free endpoint, so the
#: fallback costs one wasted request per process, not one per symbol.
_adjusted_supported: bool | None = None


class AlphaVantageProviderError(RuntimeError):
    """Alpha Vantage rejected the request or returned a provider-side failure."""


class AlphaVantageQuotaError(AlphaVantageProviderError):
    """The daily/minute request quota is exhausted — further calls can only fail."""


def _instrument_defaults(av_symbol: str) -> tuple[str, str, str]:
    """Map Alpha Vantage symbols to the canonical instrument key (same convention as fmp.py,
    but Toronto listings are ``SHOP.TRT`` on Alpha Vantage rather than ``SHOP.TO``)."""
    symbol = av_symbol.strip().upper()
    if symbol.endswith(".TRT"):
        return symbol.removesuffix(".TRT"), "TSX", "CAD"
    if symbol.endswith(".TO"):
        return symbol.removesuffix(".TO"), "TSX", "CAD"
    return symbol, "SMART", "USD"


def _get_or_create_instrument(session: Session, av_symbol: str) -> Instrument:
    symbol, exchange, currency = _instrument_defaults(av_symbol)
    instrument = session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
        )
    )
    if instrument:
        return instrument

    instrument = Instrument(symbol=symbol, exchange=exchange, currency=currency)
    session.add(instrument)
    session.flush()
    return instrument


def _field(values: dict[str, Any], name: str) -> Any:
    """Alpha Vantage prefixes fields with ordinals that differ per endpoint
    ("4. close" vs "5. adjusted close" vs "5. volume") — match on the name only."""
    for key, value in values.items():
        if key.split(". ", 1)[-1] == name:
            return value
    return None


def _daily_ts(value: str) -> datetime:
    day = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(day, time.min, tzinfo=UTC)


def _bar_values(day: str, values: dict[str, Any], adjusted: bool) -> dict[str, Any]:
    try:
        close = float(_field(values, "close"))
        open_ = float(_field(values, "open"))
        high = float(_field(values, "high"))
        low = float(_field(values, "low"))
        volume = _field(values, "volume")
        factor = 1.0
        if adjusted:
            adjusted_close = float(_field(values, "adjusted close"))
            factor = adjusted_close / close if close else 1.0
        return {
            "ts": _daily_ts(day),
            "open": open_ * factor,
            "high": high * factor,
            "low": low * factor,
            "close": close * factor,
            "volume": None if volume is None else float(volume),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid Alpha Vantage daily row for {day}: {values!r}") from exc


def _provider_message(payload: dict[str, Any]) -> str:
    message = payload.get("Information") or payload.get("Note") or ""
    return message if isinstance(message, str) else ""


def _raise_on_quota(payload: dict[str, Any]) -> None:
    """Quota-exhaustion bodies mention the rate limit ("...standard API rate limit is 25
    requests per day..."); checked before the premium test because those bodies often also
    advertise premium plans."""
    message = _provider_message(payload)
    lowered = message.lower()
    if SERIES_KEY not in payload and ("rate limit" in lowered or "call frequency" in lowered):
        raise AlphaVantageQuotaError(f"Alpha Vantage quota exhausted: {message}")


def _is_premium_refusal(payload: dict[str, Any]) -> bool:
    return "premium" in _provider_message(payload).lower()


class AlphaVantageConnector(Connector):
    name = "alpha_vantage"

    def fetch(self, symbol: str = "", outputsize: str = "compact", **kwargs) -> int:
        global _adjusted_supported
        settings = self.settings
        if not settings.alpha_vantage_key:
            raise RuntimeError("ALPHA_VANTAGE_KEY is not set (see .env.example)")
        av_symbol = symbol.strip().upper()
        if not av_symbol:
            raise ValueError("symbol is required")

        adjusted = _adjusted_supported is not False
        payload: dict[str, Any] | None = None
        if adjusted:
            payload = _query(
                function="TIME_SERIES_DAILY_ADJUSTED",
                symbol=av_symbol,
                outputsize=outputsize,
                api_key=settings.alpha_vantage_key,
            )
            _raise_on_quota(payload)
            if _is_premium_refusal(payload):
                _adjusted_supported = False
                adjusted = False
                payload = None
        if payload is None:
            payload = _query(
                function="TIME_SERIES_DAILY",
                symbol=av_symbol,
                outputsize=outputsize,
                api_key=settings.alpha_vantage_key,
            )
            _raise_on_quota(payload)

        _raise_on_provider_message(payload)
        series = payload.get(SERIES_KEY)
        if not isinstance(series, dict):
            raise AlphaVantageProviderError("unexpected Alpha Vantage daily response shape")

        what_to_show = "ADJUSTED_LAST" if adjusted else "TRADES"
        with self.session() as session:
            instrument = _get_or_create_instrument(session, av_symbol)
            count = 0
            for day in sorted(series):
                values = _bar_values(day, series[day], adjusted=adjusted)
                existing = session.scalar(
                    select(PriceBar).where(
                        PriceBar.instrument_id == instrument.id,
                        PriceBar.ts == values["ts"],
                        PriceBar.bar_size == BAR_SIZE,
                        PriceBar.source == self.name,
                        PriceBar.what_to_show == what_to_show,
                    )
                )
                if existing:
                    existing.open = values["open"]
                    existing.high = values["high"]
                    existing.low = values["low"]
                    existing.close = values["close"]
                    existing.volume = values["volume"]
                else:
                    session.add(
                        PriceBar(
                            instrument_id=instrument.id,
                            ts=values["ts"],
                            bar_size=BAR_SIZE,
                            source=self.name,
                            what_to_show=what_to_show,
                            open=values["open"],
                            high=values["high"],
                            low=values["low"],
                            close=values["close"],
                            volume=values["volume"],
                        )
                    )
                count += 1
            return count


def read_tickers_file(path: str) -> list[str]:
    """One Alpha Vantage symbol per line; blank lines and ``#`` comments are skipped.

    Keep the file small — the free tier is ~25 requests/day, so more than ~20 symbols can
    never complete in one run anyway.
    """
    symbols: list[str] = []
    with open(path, encoding="utf-8-sig") as handle:  # -sig: tolerate a Notepad BOM
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            symbol = stripped.upper()
            if symbol not in symbols:
                symbols.append(symbol)
    return symbols


def _has_fresh_bars(session: Session, av_symbol: str, refresh_after_days: float) -> bool:
    """True when the newest stored alpha_vantage bar for the symbol is younger than the
    cutoff — a re-fetch would only overwrite what's already there. ``what_to_show`` is
    ignored on purpose: adjusted and fallback-unadjusted bars both count as downloaded."""
    symbol, exchange, currency = _instrument_defaults(av_symbol)
    newest = session.scalar(
        select(func.max(PriceBar.ts))
        .join(Instrument, PriceBar.instrument_id == Instrument.id)
        .where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
            PriceBar.bar_size == BAR_SIZE,
            PriceBar.source == AlphaVantageConnector.name,
        )
    )
    if newest is None:
        return False
    if newest.tzinfo is None:  # SQLite drops tzinfo on readback (Postgres keeps it)
        newest = newest.replace(tzinfo=UTC)
    return newest >= datetime.now(UTC) - timedelta(days=refresh_after_days)


def fetch_universe(
    symbols: list[str],
    refresh_after_days: float = 1.0,
    max_requests: int = 20,
    spacing_seconds: float = 1.0,
    sleep: Callable[[float], None] = time_mod.sleep,
    session_factory: SessionFactory | None = None,
) -> int:
    """Fetch daily bars for a symbol list under the free-tier budget.

    Free-tier protections, in order:
    - **freshness skip**: symbols whose newest stored bar is younger than
      ``refresh_after_days`` cost nothing (default 1 day — same-day reruns are free; set 3
      to also skip weekend runs, 0 to force a full re-fetch);
    - **request budget**: at most ``max_requests`` fetches per run (free tier is ~25/day —
      the default leaves headroom; the first fetch of a process may cost one extra HTTP
      call probing the premium adjusted endpoint);
    - **quota abort**: ``AlphaVantageQuotaError`` stops the run — once the quota is gone,
      every remaining call fails identically. Other per-symbol failures (bad ticker) are
      logged and skipped, and stay stale for the next run.
    """
    session_factory = resolve_session_factory(session_factory)
    with session_factory() as session:
        stale = [
            s
            for s in symbols
            if refresh_after_days <= 0 or not _has_fresh_bars(session, s, refresh_after_days)
        ]
    skipped_fresh = len(symbols) - len(stale)

    connector = AlphaVantageConnector(session_factory=session_factory)
    total = 0
    requests_used = 0
    for index, av_symbol in enumerate(stale):
        if requests_used >= max_requests:
            logger.warning(
                "alpha_vantage batch stopped at the %d-request budget; %d symbol(s) left "
                "for a later run",
                max_requests,
                len(stale) - index,
            )
            break
        if requests_used:
            sleep(spacing_seconds)
        requests_used += 1
        try:
            total += connector.fetch(symbol=av_symbol)
        except AlphaVantageQuotaError as exc:
            logger.error(
                "alpha_vantage batch aborted at %s: %s — remaining %d symbol(s) would fail too",
                av_symbol,
                exc,
                len(stale) - index - 1,
            )
            break
        except Exception:
            logger.exception("alpha_vantage batch failed for %s", av_symbol)
    logger.info(
        "alpha_vantage batch upserted %d bars across %d symbols (%d requests, %d fresh-skipped)",
        total,
        len(symbols),
        requests_used,
        skipped_fresh,
    )
    return total


def _raise_on_provider_message(payload: dict[str, Any]) -> None:
    """Surface Alpha Vantage's HTTP-200 failure bodies (bad symbol, daily quota, ...)."""
    for key in ("Error Message", "Note", "Information"):
        message = payload.get(key)
        if message and SERIES_KEY not in payload:
            raise AlphaVantageProviderError(f"Alpha Vantage refused the request: {message}")


def _query(function: str, symbol: str, outputsize: str, api_key: str) -> dict[str, Any]:
    response = httpx.get(
        BASE_URL,
        params={
            "function": function,
            "symbol": symbol,
            "outputsize": outputsize,
            "apikey": api_key,
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise AlphaVantageProviderError(
            f"Alpha Vantage request failed with HTTP {status}. Check ALPHA_VANTAGE_KEY and the "
            "daily quota (~25 req/day on free keys). API key omitted from error."
        ) from None
    payload = response.json()
    if not isinstance(payload, dict):
        raise AlphaVantageProviderError("unexpected Alpha Vantage response shape")
    return payload
