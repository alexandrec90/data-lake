"""Shared yfinance plumbing for the Yahoo connectors (prices + fundamentals).

yfinance is an unofficial scraper with no official API, key, or documented quota — Yahoo just
temp-bans IPs that hammer it. The throttle state below is **module-level on purpose**: every
Yahoo connector imports it, so a single request-spacing budget is shared across prices and
fundamentals rather than each getting its own. Keep volume tiny and never loop tightly.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from datetime import time as time_of_day
from typing import TypeVar, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import Instrument, PriceBar

#: Minimum spacing between Yahoo requests. There is no documented quota — Yahoo just
#: rate-limits/bans abusive IPs, so stay far below anything that looks automated-hostile.
MIN_REQUEST_INTERVAL_SECONDS = 2.0

#: Hard wall-clock ceiling on a single yfinance call. yfinance (via curl_cffi) exposes no
#: timeout, so when Yahoo rate-limits it can hold a request open indefinitely — hanging the whole
#: batch. ``run_within_timeout`` abandons a request past this and raises, so a batch runner
#: reports a per-symbol failure and moves on instead of stalling forever.
DOWNLOAD_TIMEOUT_SECONDS = 45.0

_last_request_monotonic: float | None = None

T = TypeVar("T")


class YahooProviderError(RuntimeError):
    """Yahoo rejected the request or returned no usable data."""


def throttle() -> None:
    """Sleep as needed so consecutive Yahoo requests are at least the min interval apart."""
    global _last_request_monotonic
    now = time.monotonic()
    if _last_request_monotonic is not None:
        wait = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_monotonic)
        if wait > 0:
            time.sleep(wait)
    _last_request_monotonic = time.monotonic()


def instrument_defaults(yahoo_symbol: str) -> tuple[str, str, str]:
    """Map Yahoo symbols to the canonical instrument key (same convention as fmp.py)."""
    symbol = yahoo_symbol.strip().upper()
    if symbol.endswith(".TO"):
        return symbol.removesuffix(".TO"), "TSX", "CAD"
    return symbol, "SMART", "USD"


def daily_ts(day: date) -> datetime:
    return datetime.combine(day, time_of_day.min, tzinfo=UTC)


def get_or_create_instrument(session: Session, yahoo_symbol: str) -> Instrument:
    symbol, exchange, currency = instrument_defaults(yahoo_symbol)
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


def get_instrument(session: Session, yahoo_symbol: str) -> Instrument | None:
    symbol, exchange, currency = instrument_defaults(yahoo_symbol)
    return session.scalar(
        select(Instrument).where(
            Instrument.symbol == symbol,
            Instrument.exchange == exchange,
            Instrument.currency == currency,
        )
    )


def yahoo_symbol(instrument: Instrument) -> str:
    """Inverse of ``instrument_defaults``: the Yahoo ticker for a stored instrument."""
    return f"{instrument.symbol}.TO" if instrument.exchange == "TSX" else instrument.symbol


def tracked_yahoo_symbols(session: Session) -> list[str]:
    """Yahoo tickers of every instrument that already has yahoo-source price bars.

    This is the "refresh what we track" universe for the scheduled price poll: it never
    guesses ticker formats or reads universe files — an instrument enters it via a first
    manual/batch Yahoo ingest and stays current from then on.
    """
    instruments = session.scalars(
        select(Instrument)
        .join(PriceBar, PriceBar.instrument_id == Instrument.id)
        # CASH (FX) instruments also carry yahoo-source bars, but their Yahoo ticker is
        # "PAIR=X", not the equity format — they are refreshed by scheduler.poll_fx via
        # yahoo_fx.YahooFxConnector, never by the equity price poll.
        .where(PriceBar.source == "yahoo", Instrument.sec_type != "CASH")
        .distinct()
        .order_by(Instrument.symbol)
    ).all()
    return [yahoo_symbol(instrument) for instrument in instruments]


def run_within_timeout(work: Callable[[], T], *, timeout: float, label: str) -> T:
    """Run ``work`` under a wall-clock ceiling; raise ``YahooProviderError`` if it overruns.

    The work runs in a daemon thread: if it overruns we abandon it (it dies with the process)
    rather than blocking on ``join``. yfinance has no timeout knob, so this is the only reliable
    way to bound it. Tests stub the network layer, so they return well within the ceiling.
    """
    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # re-raised on the caller thread below
            box["error"] = exc

    worker = threading.Thread(target=_worker, name=f"yahoo-{label}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise YahooProviderError(
            f"Yahoo request for {label} exceeded {timeout:.0f}s (likely rate-limited) — abandoned"
        )
    if "error" in box:
        error = box["error"]
        assert isinstance(error, BaseException)
        raise YahooProviderError(f"Yahoo request for {label} failed: {error}") from error
    return cast(T, box["value"])
