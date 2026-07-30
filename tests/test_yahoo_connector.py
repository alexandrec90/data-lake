from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market import yahoo, yahoo_common


def _make_session_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def _frame(rows: dict[str, list], dates: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates, tz="America/Toronto"))


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(yahoo_common, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(yahoo_common, "_last_request_monotonic", None)


def test_yahoo_fetch_upserts_price_bars_and_maps_to_suffix(monkeypatch):
    frame = _frame(
        {
            "Open": [30.0, 30.5],
            "High": [30.6, 31.0],
            "Low": [29.8, 30.2],
            "Close": [30.4, 30.9],
            "Volume": [100_000, float("nan")],
        },
        ["2024-01-02", "2024-01-03"],
    )
    calls: list[tuple] = []
    session_cm = _make_session_scope()

    def fake_download(symbol, start, end, auto_adjust):
        calls.append((symbol, start, end, auto_adjust))
        return frame

    monkeypatch.setattr(yahoo, "_download_history", fake_download)

    count = yahoo.YahooConnector(session_factory=session_cm).fetch(symbol="xeqt.to")

    assert count == 2
    assert calls == [("XEQT.TO", None, None, True)]
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "XEQT"))
        assert instrument is not None
        assert instrument.exchange == "TSX"
        assert instrument.currency == "CAD"
        bars = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == instrument.id).order_by(PriceBar.ts)
        ).all()
        assert len(bars) == 2
        assert bars[0].source == "yahoo"
        assert bars[0].what_to_show == "ADJUSTED_LAST"
        assert bars[0].bar_size == "1 day"
        assert bars[0].ts.date() == date(2024, 1, 2)
        assert bars[0].open == 30.0
        assert bars[0].volume == 100_000.0
        assert bars[1].volume is None


def test_yahoo_fetch_starts_from_next_missing_date(monkeypatch):
    frames = [
        _frame(
            {"Open": [30.0], "High": [30.6], "Low": [29.8], "Close": [30.4], "Volume": [1.0]},
            ["2024-01-02"],
        ),
        _frame(
            {"Open": [30.5], "High": [31.0], "Low": [30.2], "Close": [30.9], "Volume": [2.0]},
            ["2024-01-03"],
        ),
    ]
    starts: list = []
    session_cm = _make_session_scope()

    def fake_download(symbol, start, end, auto_adjust):
        starts.append(start)
        return frames.pop(0)

    monkeypatch.setattr(yahoo, "_download_history", fake_download)

    assert yahoo.YahooConnector(session_factory=session_cm).fetch(symbol="XEQT.TO") == 1
    assert yahoo.YahooConnector(session_factory=session_cm).fetch(symbol="XEQT.TO") == 1

    assert starts == [None, date(2024, 1, 3)]
    with session_cm() as session:
        assert len(session.scalars(select(PriceBar)).all()) == 2


def test_yahoo_fetch_updates_existing_price_bar(monkeypatch):
    frames = [
        _frame(
            {"Open": [30.0], "High": [30.6], "Low": [29.8], "Close": [30.4], "Volume": [1.0]},
            ["2024-01-02"],
        ),
        _frame(
            {"Open": [30.0], "High": [30.8], "Low": [29.8], "Close": [30.5], "Volume": [1.0]},
            ["2024-01-02"],
        ),
    ]
    session_cm = _make_session_scope()

    monkeypatch.setattr(yahoo, "_download_history", lambda *a, **k: frames.pop(0))

    assert yahoo.YahooConnector(session_factory=session_cm).fetch(symbol="XEQT.TO") == 1
    assert (
        yahoo.YahooConnector(session_factory=session_cm).fetch(
            symbol="XEQT.TO", date_from="2024-01-02"
        )
        == 1
    )

    with session_cm() as session:
        bars = session.scalars(select(PriceBar)).all()
        assert len(bars) == 1
        assert bars[0].high == 30.8
        assert bars[0].close == 30.5


def test_yahoo_fetch_skips_request_when_local_data_covers_date_to(monkeypatch):
    session_cm = _make_session_scope()

    def fail_download(*args, **kwargs):
        raise AssertionError("Yahoo should not be called when local data already covers date_to")

    monkeypatch.setattr(yahoo, "_download_history", fail_download)

    with session_cm() as session:
        instrument = Instrument(symbol="XEQT", exchange="TSX", currency="CAD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=yahoo._daily_ts(date(2024, 1, 2)),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=30.0,
                high=30.6,
                low=29.8,
                close=30.4,
                volume=1.0,
            )
        )

    assert (
        yahoo.YahooConnector(session_factory=session_cm).fetch(
            symbol="XEQT.TO", date_to="2024-01-02"
        )
        == 0
    )


def test_yahoo_fetch_trades_disables_auto_adjust_and_passes_exclusive_end(monkeypatch):
    calls: list[tuple] = []
    session_cm = _make_session_scope()

    def fake_download(symbol, start, end, auto_adjust):
        calls.append((symbol, start, end, auto_adjust))
        return _frame(
            {"Open": [30.0], "High": [30.6], "Low": [29.8], "Close": [30.4], "Volume": [1.0]},
            ["2024-01-02"],
        )

    monkeypatch.setattr(yahoo, "_download_history", fake_download)

    connector = yahoo.YahooConnector(session_factory=session_cm)
    count = connector.fetch(
        symbol="GOOG", date_from="2024-01-02", date_to="2024-01-05", what_to_show="TRADES"
    )

    assert count == 1
    # yfinance treats `end` as exclusive, so an inclusive date_to becomes date_to + 1 day
    assert calls == [("GOOG", date(2024, 1, 2), date(2024, 1, 6), False)]
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "GOOG"))
        assert instrument is not None
        assert instrument.exchange == "SMART"
        assert instrument.currency == "USD"
        bar = session.scalar(select(PriceBar))
        assert bar is not None
        assert bar.what_to_show == "TRADES"


def test_yahoo_fetch_skips_request_when_already_current(monkeypatch):
    """DB current through today → next missing date is in the future → don't call Yahoo."""
    from datetime import UTC, datetime

    session_cm = _make_session_scope()

    def fail_download(*args, **kwargs):
        raise AssertionError("Yahoo should not be called when the next missing date is future")

    monkeypatch.setattr(yahoo, "_download_history", fail_download)

    today = datetime.now(tz=UTC).date()
    with session_cm() as session:
        instrument = Instrument(symbol="XEQT", exchange="TSX", currency="CAD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=yahoo._daily_ts(today),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=30.0,
                high=30.6,
                low=29.8,
                close=30.4,
                volume=1.0,
            )
        )

    assert yahoo.YahooConnector(session_factory=session_cm).fetch(symbol="XEQT.TO") == 0


def test_yahoo_fetch_wraps_provider_errors(monkeypatch):
    session_cm = _make_session_scope()

    def fake_download(*args, **kwargs):
        raise RuntimeError("possibly rate limited")

    monkeypatch.setattr(yahoo, "_download_history", fake_download)

    with pytest.raises(yahoo.YahooProviderError) as exc_info:
        yahoo.YahooConnector(session_factory=session_cm).fetch(symbol="XEQT.TO")
    assert "XEQT.TO" in str(exc_info.value)


def test_download_within_timeout_raises_on_hang(monkeypatch):
    """A download that outruns the ceiling is abandoned and surfaced as a provider error,
    rather than hanging the batch (yfinance has no timeout of its own)."""
    import time as _time

    def slow_download(symbol, start, end, auto_adjust):
        _time.sleep(5.0)  # never returns within the tiny ceiling below
        raise AssertionError("should have been abandoned")

    monkeypatch.setattr(yahoo, "_download_history", slow_download)

    with pytest.raises(yahoo.YahooProviderError) as exc_info:
        yahoo._download_within_timeout("XOM", None, None, True, timeout=0.2)
    assert "XOM" in str(exc_info.value)
    assert "0s" in str(exc_info.value) or "exceeded" in str(exc_info.value)


def test_throttle_spaces_out_consecutive_requests(monkeypatch):
    clock = {"now": 0.0}
    sleeps: list[float] = []

    monkeypatch.setattr(yahoo_common, "MIN_REQUEST_INTERVAL_SECONDS", 2.0)
    monkeypatch.setattr(yahoo_common, "_last_request_monotonic", None)
    monkeypatch.setattr(yahoo_common.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(yahoo_common.time, "sleep", sleeps.append)

    yahoo._throttle()
    assert sleeps == []  # first request goes straight through

    clock["now"] = 0.5
    yahoo._throttle()
    assert sleeps == [1.5]  # only 0.5s elapsed → wait the remaining 1.5s


def test_tracked_yahoo_symbols_returns_only_yahoo_sourced_instruments():
    session_cm = _make_session_scope()
    with session_cm() as session:
        tsx = Instrument(symbol="XEQT", exchange="TSX", currency="CAD")
        us = Instrument(symbol="AAPL", exchange="SMART", currency="USD")
        fmp_only = Instrument(symbol="NVDA", exchange="SMART", currency="USD")
        bare = Instrument(symbol="ZZZ", exchange="SMART", currency="USD")  # no bars at all
        # FX CASH instruments carry yahoo bars too (yahoo_fx backfill) but their Yahoo ticker
        # is "PAIR=X" — the equity poll must never pick them up.
        cash = Instrument(symbol="USDCAD", exchange="IDEALPRO", currency="CAD", sec_type="CASH")
        session.add_all([tsx, us, fmp_only, bare, cash])
        session.flush()

        def bar(instrument, source, day):
            return PriceBar(
                instrument_id=instrument.id,
                ts=yahoo._daily_ts(day),
                bar_size="1 day",
                source=source,
                what_to_show="ADJUSTED_LAST",
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )

        session.add_all(
            [
                bar(tsx, "yahoo", date(2024, 1, 2)),
                bar(tsx, "yahoo", date(2024, 1, 3)),  # two bars, must not duplicate
                bar(us, "yahoo", date(2024, 1, 2)),
                bar(fmp_only, "fmp", date(2024, 1, 2)),
                bar(cash, "yahoo", date(2024, 1, 2)),
            ]
        )

    with session_cm() as session:
        symbols = yahoo_common.tracked_yahoo_symbols(session)

    # TSX names get their Yahoo suffix back; fmp-only, bar-less and CASH (FX) instruments
    # are excluded
    assert symbols == ["AAPL", "XEQT.TO"]


def test_yahoo_symbol_round_trips_instrument_defaults():
    tsx = Instrument(symbol="XEQT", exchange="TSX", currency="CAD")
    us = Instrument(symbol="AAPL", exchange="SMART", currency="USD")
    assert yahoo_common.yahoo_symbol(tsx) == "XEQT.TO"
    assert yahoo_common.yahoo_symbol(us) == "AAPL"
    # inverse property: defaults(symbol(i)) == the instrument key
    assert yahoo_common.instrument_defaults(yahoo_common.yahoo_symbol(tsx)) == (
        "XEQT",
        "TSX",
        "CAD",
    )
