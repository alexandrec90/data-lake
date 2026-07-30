"""FMP foreign-exchange connector — daily FX rates for the CAD reporting currency.

Stores one CASH instrument per pair (e.g. ``USDCAD``, where close = CAD per 1 USD) so the
backtester can value US-priced holdings in CAD (`backtest.engine._fx_to_cad`) and charge the
CAD↔USD conversion cost when the currency mix shifts.

Reuses the FMP HTTP + row-parsing helpers from the price connector ([fmp.py](fmp.py)); FX uses
the same EOD endpoint with a pair symbol. **[verify]** FMP's EOD endpoint coverage for FX pairs
against your plan — fall back to a dedicated FX endpoint if the pair returns empty.
"""

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.base import Connector
from data_lake.ingestion.market import fmp

DEFAULT_PAIR = "USDCAD"


def _quote_currency(pair: str) -> str:
    """Quote currency of a 6-letter pair: USDCAD → CAD. Falls back to CAD if malformed."""
    return pair[3:6].upper() if len(pair) >= 6 else "CAD"


def get_or_create_fx_instrument(session: Session, pair: str) -> Instrument:
    """The CASH instrument for a pair — shared with the Yahoo FX connector (yahoo_fx.py) so
    both providers write bars against one instrument row."""
    symbol = pair.upper()
    instrument = session.scalar(
        select(Instrument).where(Instrument.symbol == symbol, Instrument.sec_type == "CASH")
    )
    if instrument:
        return instrument
    instrument = Instrument(
        symbol=symbol,
        exchange="IDEALPRO",
        currency=_quote_currency(symbol),
        sec_type="CASH",
        name=f"{symbol[:3]}/{symbol[3:6]}",
    )
    session.add(instrument)
    session.flush()
    return instrument


class FmpFxConnector(Connector):
    name = "fmp"  # stored in price_bars.source, consistent with the equity price connector

    def fetch(
        self,
        pair: str = DEFAULT_PAIR,
        date_from: str = "",
        date_to: str = "",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        **kwargs,
    ) -> int:
        settings = self.settings
        if not settings.fmp_key:
            raise RuntimeError("FMP_KEY is not set (see .env.example)")
        fx_symbol = pair.strip().upper()
        if not fx_symbol:
            raise ValueError("pair is required (e.g. USDCAD)")

        params = {"symbol": fx_symbol, "apikey": settings.fmp_key}
        if date_from:
            params["from"] = date_from
        if date_to:
            params["to"] = date_to

        response = fmp._get_fmp_eod(params=params, use_light_endpoint=False)
        if response.status_code == 402:
            response = fmp._get_fmp_eod(params=params, use_light_endpoint=True)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise fmp.FmpProviderError(
                f"FMP FX request failed with HTTP {status}. Check FMP_KEY, plan access, and "
                "quota. API key omitted from error."
            ) from None
        rows = sorted(
            fmp._historical_rows(response.json()), key=lambda row: str(row.get("date", ""))
        )

        with self.session() as session:
            instrument = get_or_create_fx_instrument(session, fx_symbol)
            count = 0
            for row in rows:
                values = fmp._bar_values(row)
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
