"""Financial Modeling Prep connector — EOD prices, fundamentals, calendars.

Free tier ~250 req/day. Docs:
https://site.financialmodelingprep.com/developer/docs/stable/historical-price-eod-full
"""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.base import Connector

BASE_URL = "https://financialmodelingprep.com/stable"
FULL_EOD_PATH = "/historical-price-eod/full"
LIGHT_EOD_PATH = "/historical-price-eod/light"


class FmpProviderError(RuntimeError):
    """FMP rejected the request or returned a provider-side failure."""


def _instrument_defaults(fmp_symbol: str) -> tuple[str, str, str]:
    """Map common FMP symbols to the canonical instrument key used locally."""
    symbol = fmp_symbol.strip().upper()
    if symbol.endswith(".TO"):
        return symbol.removesuffix(".TO"), "TSX", "CAD"
    return symbol, "SMART", "USD"


def _daily_ts(value: str) -> datetime:
    day = datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.combine(day, time.min, tzinfo=UTC)


def _date_from_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _historical_rows(payload: Any) -> list[dict[str, Any]]:
    """Accept both current stable responses and the older v3 envelope shape."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("historical"), list):
        rows = payload["historical"]
    else:
        raise ValueError("unexpected FMP historical-price response shape")

    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("unexpected FMP historical-price row shape")
    return rows


def _bar_values(row: dict[str, Any]) -> dict[str, Any]:
    try:
        volume = row.get("volume")
        close = row["close"] if "close" in row else row["price"]
        open_ = row.get("open", close)
        high = row.get("high", close)
        low = row.get("low", close)
        return {
            "ts": _daily_ts(str(row["date"])),
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": None if volume is None else float(volume),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid FMP price bar row: {row!r}") from exc


def _get_or_create_instrument(session: Session, fmp_symbol: str) -> Instrument:
    symbol, exchange, currency = _instrument_defaults(fmp_symbol)
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


def _get_instrument(session: Session, fmp_symbol: str) -> Instrument | None:
    symbol, exchange, currency = _instrument_defaults(fmp_symbol)
    return session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
        )
    )


def _next_missing_date(
    session: Session,
    instrument: Instrument,
    bar_size: str,
    source: str,
    what_to_show: str,
) -> date | None:
    latest_ts = session.scalar(
        select(func.max(PriceBar.ts)).where(
            PriceBar.instrument_id == instrument.id,
            PriceBar.bar_size == bar_size,
            PriceBar.source == source,
            PriceBar.what_to_show == what_to_show,
        )
    )
    if latest_ts is None:
        return None
    return latest_ts.date() + timedelta(days=1)


class FmpConnector(Connector):
    name = "fmp"

    def fetch(
        self,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        **kwargs,
    ) -> int:
        settings = self.settings
        if not settings.fmp_key:
            raise RuntimeError("FMP_KEY is not set (see .env.example)")
        fmp_symbol = symbol.strip().upper()
        if not fmp_symbol:
            raise ValueError("symbol is required")

        params = {"symbol": fmp_symbol, "apikey": settings.fmp_key}
        effective_date_from = date_from
        if not effective_date_from:
            with self.session() as session:
                instrument = _get_instrument(session, fmp_symbol)
                if instrument:
                    next_missing_date = _next_missing_date(
                        session=session,
                        instrument=instrument,
                        bar_size=bar_size,
                        source=self.name,
                        what_to_show=what_to_show,
                    )
                    if next_missing_date:
                        effective_date_from = next_missing_date.isoformat()

        if effective_date_from:
            params["from"] = effective_date_from
        if date_to:
            params["to"] = date_to
        if (
            effective_date_from
            and date_to
            and _date_from_ymd(effective_date_from) > _date_from_ymd(date_to)
        ):
            return 0

        response = _get_fmp_eod(params=params, use_light_endpoint=False)
        if response.status_code == 402:
            response = _get_fmp_eod(params=params, use_light_endpoint=True)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise FmpProviderError(
                f"FMP request failed with HTTP {status}. Check FMP_KEY, plan access, "
                "quota, and the FMP dashboard. API key omitted from error."
            ) from None
        rows = sorted(_historical_rows(response.json()), key=lambda row: str(row.get("date", "")))

        with self.session() as session:
            instrument = _get_or_create_instrument(session, fmp_symbol)
            count = 0
            for row in rows:
                values = _bar_values(row)
                existing = session.scalar(
                    select(PriceBar).where(
                        PriceBar.instrument_id == instrument.id,
                        PriceBar.ts == values["ts"],
                        PriceBar.bar_size == bar_size,
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
                            bar_size=bar_size,
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


def _get_fmp_eod(params: dict[str, str], use_light_endpoint: bool) -> httpx.Response:
    path = LIGHT_EOD_PATH if use_light_endpoint else FULL_EOD_PATH
    api_key = params["apikey"]
    request_params = {key: value for key, value in params.items() if key != "apikey"}
    return httpx.get(
        f"{BASE_URL}{path}",
        params=request_params,
        headers={"apikey": api_key},
        timeout=30,
    )
