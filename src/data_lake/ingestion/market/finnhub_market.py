"""Finnhub market-data connector — quotes and candles (separate from finnhub_news)."""

from data_lake.ingestion.base import Connector


class FinnhubMarketConnector(Connector):
    name = "finnhub"

    def fetch(self, symbol: str = "", resolution: str = "D", **kwargs) -> int:
        settings = self.settings
        if not settings.finnhub_key:
            raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")

        # TODO(skeleton): GET /stock/candle?symbol=&resolution=&from=&to= → PriceBar rows.
        # NOTE [verify]: candle endpoint access on the current free tier — Finnhub moved
        # candles to paid plans for some exchanges at some point.
        raise NotImplementedError("not implemented yet")
