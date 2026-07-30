"""IBKR historical-bars connector via ib_async.

Highest-fidelity source (matches the broker we trade with) but strict pacing rules —
budget ~1 request / 10 s sustained unless current official IBKR rules say otherwise.
"""

from data_lake.ingestion.base import Connector


class IbkrHistoricalConnector(Connector):
    name = "ibkr"

    def fetch(
        self,
        symbol: str = "",
        duration: str = "1 Y",
        bar_size: str = "1 day",
        what_to_show: str = "ADJUSTED_LAST",
        **kwargs,
    ) -> int:
        settings = self.settings
        _ = settings  # host/port/client_id come from settings when wired

        # TODO(skeleton):
        #   from ib_async import IB, Stock
        #   ib = IB(); ib.connect(settings.ibkr_host, settings.ibkr_port,
        #                         clientId=settings.ibkr_client_id, readonly=True)
        #   contract = Stock(symbol, "SMART", "USD"); ib.qualifyContracts(contract)
        #   bars = ib.reqHistoricalData(contract, endDateTime="", durationStr=duration,
        #                               barSizeSetting=bar_size, whatToShow=what_to_show,
        #                               useRTH=True, formatDate=2)
        #   → upsert PriceBar rows (source="ibkr"); cache contract.conId on Instrument.
        # Throttle: token bucket ≤ 6/min; identical-request cooldown ≥ 15 s (pacing rules).
        raise NotImplementedError("ib_async wiring not implemented yet")
