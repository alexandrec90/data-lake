"""Yahoo FX connector — deep-history daily FX for the CAD reporting currency.

FMP's free tier caps EOD history at ~5 years, which pinned every simulation window to
2021-07-08 (the oldest USDCAD bar it would serve). Yahoo carries major pairs (``USDCAD=X``)
back to ~2003, so this connector backfills and refreshes the **same CASH instrument** the FMP
FX connector maintains, just under ``source="yahoo"``. The engine loads each instrument's bars
from exactly one provider per window (``backtest.engine.SOURCE_PREFERENCE``, widest coverage
wins), so the two sources coexist without double-counting.

FX bars store ``what_to_show="TRADES"`` — there is no dividend/split adjustment for
currencies — landing in the same bucket as the FMP FX bars so the loader's TRADES fallback
finds them.

Same yfinance caveats as [yahoo.py](yahoo.py): unofficial scraper, shared module-level
throttle, hard per-request timeout.
"""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.base import Connector, SessionFactory, resolve_session_factory
from data_lake.ingestion.market.fmp_fx import DEFAULT_PAIR, get_or_create_fx_instrument
from data_lake.ingestion.market.yahoo import (
    _bar_values,
    _date_from_ymd,
    _download_within_timeout,
)
from data_lake.ingestion.market.yahoo_common import DOWNLOAD_TIMEOUT_SECONDS
from data_lake.ingestion.market.yahoo_common import throttle as _throttle

BAR_SIZE = "1 day"
WHAT_TO_SHOW = "TRADES"


def _next_missing_date(pair: str, session_factory: SessionFactory | None = None) -> date | None:
    """Day after the newest yahoo-source bar of the pair's CASH instrument (None = no bars)."""
    with resolve_session_factory(session_factory)() as session:
        latest_ts = session.scalar(
            select(func.max(PriceBar.ts))
            .join(Instrument, Instrument.id == PriceBar.instrument_id)
            .where(
                Instrument.symbol == pair,
                Instrument.sec_type == "CASH",
                PriceBar.bar_size == BAR_SIZE,
                PriceBar.source == "yahoo",
                PriceBar.what_to_show == WHAT_TO_SHOW,
            )
        )
    if latest_ts is None:
        return None
    return latest_ts.date() + timedelta(days=1)


class YahooFxConnector(Connector):
    name = "yahoo"  # stored in price_bars.source, consistent with the equity Yahoo connector

    def fetch(
        self,
        pair: str = DEFAULT_PAIR,
        date_from: str = "",
        date_to: str = "",
        **kwargs,
    ) -> int:
        """Upsert daily FX bars for ``pair`` (e.g. USDCAD, close = CAD per 1 USD).

        Without ``date_from``, resumes from the newest stored yahoo-source bar — or fetches
        Yahoo's full history (``period="max"``) when this source has none yet, which is the
        one-time deep backfill.
        """
        fx_symbol = pair.strip().upper()
        if not fx_symbol:
            raise ValueError("pair is required (e.g. USDCAD)")

        start: date | None = (
            _date_from_ymd(date_from)
            if date_from
            else _next_missing_date(fx_symbol, self.session_factory)
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
            f"{fx_symbol}=X",
            start,
            end,
            auto_adjust=False,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        with self.session() as session:
            instrument = get_or_create_fx_instrument(session, fx_symbol)
            count = 0
            for values in _bar_values(frame):
                # Yahoo's older FX bars occasionally report close outside [low, high] (up to
                # ~1% in 2004-2010 USDCAD). The simulator only ever reads FX close, so widen
                # high/low to contain open/close instead of storing an inconsistent candle.
                values["high"] = max(values["high"], values["open"], values["close"])
                values["low"] = min(values["low"], values["open"], values["close"])
                existing = session.scalar(
                    select(PriceBar).where(
                        PriceBar.instrument_id == instrument.id,
                        PriceBar.ts == values["ts"],
                        PriceBar.bar_size == BAR_SIZE,
                        PriceBar.source == self.name,
                        PriceBar.what_to_show == WHAT_TO_SHOW,
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
                            what_to_show=WHAT_TO_SHOW,
                            open=values["open"],
                            high=values["high"],
                            low=values["low"],
                            close=values["close"],
                            volume=values["volume"],
                        )
                    )
                count += 1
            return count
