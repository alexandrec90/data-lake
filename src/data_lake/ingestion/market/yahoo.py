"""Yahoo Finance connector (via yfinance) — EOD prices for symbols FMP's free tier gates.

Yahoo has no official API: yfinance scrapes web endpoints, so treat it like pytrends —
unofficial and fragile. Yahoo temp-bans IPs that hammer it, hence the mandatory throttle
below. Keep volume tiny (a handful of symbols, once a day) and never put this connector
in a tight loop. Coverage includes TSX (`.TO` suffix), which FMP's free tier lacks.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.base import Connector
from data_lake.ingestion.market.yahoo_common import (
    DOWNLOAD_TIMEOUT_SECONDS,
    YahooProviderError,
    run_within_timeout,
)
from data_lake.ingestion.market.yahoo_common import daily_ts as _daily_ts
from data_lake.ingestion.market.yahoo_common import get_instrument as _get_instrument
from data_lake.ingestion.market.yahoo_common import (
    get_or_create_instrument as _get_or_create_instrument,
)
from data_lake.ingestion.market.yahoo_common import throttle as _throttle

__all__ = ["DOWNLOAD_TIMEOUT_SECONDS", "YahooConnector", "YahooProviderError"]


def _date_from_ymd(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _download_history(
    symbol: str,
    start: date | None,
    end: date | None,
    auto_adjust: bool,
) -> pd.DataFrame:
    """One yfinance history call. Isolated so tests can stub it without network."""
    import yfinance as yf
    from yfinance.exceptions import YFPricesMissingError

    try:
        return yf.Ticker(symbol).history(
            period=None if start else "max",
            interval="1d",
            start=start,
            end=end,
            auto_adjust=auto_adjust,
            actions=False,
            raise_errors=True,
        )
    except YFPricesMissingError:
        # Normal when the range holds no trading days (weekend/holiday incremental run).
        return pd.DataFrame()


def _download_within_timeout(
    symbol: str,
    start: date | None,
    end: date | None,
    auto_adjust: bool,
    timeout: float,
) -> pd.DataFrame:
    """Run ``_download_history`` under a wall-clock ceiling (see ``run_within_timeout``), so a
    request Yahoo has silently parked can't hang the batch. Tests stub ``_download_history``."""
    return run_within_timeout(
        lambda: _download_history(symbol, start, end, auto_adjust),
        timeout=timeout,
        label=symbol,
    )


def _float_or(value: Any, fallback: float | None) -> float | None:
    return fallback if value is None or pd.isna(value) else float(value)


def _bar_values(frame: pd.DataFrame) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        close = _float_or(row.get("Close"), None)
        if close is None:
            continue
        values.append(
            {
                "ts": _daily_ts(idx.date()),
                "open": _float_or(row.get("Open"), close),
                "high": _float_or(row.get("High"), close),
                "low": _float_or(row.get("Low"), close),
                "close": close,
                "volume": _float_or(row.get("Volume"), None),
            }
        )
    return values


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


class YahooConnector(Connector):
    name = "yahoo"

    def fetch(
        self,
        symbol: str = "",
        date_from: str = "",
        date_to: str = "",
        bar_size: str = "1 day",
        what_to_show: str = "ADJUSTED_LAST",
        **kwargs,
    ) -> int:
        """Upsert daily bars for one Yahoo symbol (e.g. ``XEQT.TO``, ``GOOG``).

        ``what_to_show="ADJUSTED_LAST"`` (default) stores dividend/split-adjusted bars —
        what the long-term backtest prefers; pass ``"TRADES"`` for raw prices.
        """
        yahoo_symbol = symbol.strip().upper()
        if not yahoo_symbol:
            raise ValueError("symbol is required")
        if what_to_show not in ("ADJUSTED_LAST", "TRADES"):
            raise ValueError("what_to_show must be ADJUSTED_LAST or TRADES")

        start: date | None = _date_from_ymd(date_from) if date_from else None
        if start is None:
            with self.session() as session:
                instrument = _get_instrument(session, yahoo_symbol)
                if instrument:
                    start = _next_missing_date(
                        session=session,
                        instrument=instrument,
                        bar_size=bar_size,
                        source=self.name,
                        what_to_show=what_to_show,
                    )

        if start and start > datetime.now(tz=UTC).date():
            return 0  # already current — Yahoo rejects a start date in the future

        end: date | None = None
        if date_to:
            inclusive_end = _date_from_ymd(date_to)
            if start and start > inclusive_end:
                return 0
            end = inclusive_end + timedelta(days=1)  # yfinance's `end` is exclusive

        _throttle()
        frame = _download_within_timeout(
            yahoo_symbol,
            start,
            end,
            auto_adjust=what_to_show == "ADJUSTED_LAST",
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        with self.session() as session:
            instrument = _get_or_create_instrument(session, yahoo_symbol)
            count = 0
            for values in _bar_values(frame):
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
