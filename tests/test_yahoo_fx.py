"""Tests for the Yahoo FX connector (deep-history USDCAD backfill, yahoo_fx.py)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market import yahoo_common, yahoo_fx


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


def test_fetch_backfills_full_history_into_shared_cash_instrument(monkeypatch):
    frame = _frame(
        {
            "Open": [1.32, 1.33],
            "High": [1.34, 1.34],
            "Low": [1.31, 1.32],
            "Close": [1.335, 1.331],
            "Volume": [0.0, 0.0],
        },
        ["2010-01-04", "2010-01-05"],
    )
    session_cm = _make_session_scope()
    calls: list[tuple] = []

    def fake_download(symbol, start, end, auto_adjust, timeout):
        calls.append((symbol, start, end, auto_adjust))
        return frame

    monkeypatch.setattr(yahoo_fx, "_download_within_timeout", fake_download)

    count = yahoo_fx.YahooFxConnector(session_factory=session_cm).fetch(pair="usdcad")

    assert count == 2
    # no stored yahoo bars -> full history (start=None => period="max"), never adjusted
    assert calls == [("USDCAD=X", None, None, False)]
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "USDCAD"))
        assert instrument is not None
        assert instrument.sec_type == "CASH"
        assert instrument.currency == "CAD"
        bars = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == instrument.id).order_by(PriceBar.ts)
        ).all()
        assert [round(bar.close, 3) for bar in bars] == [1.335, 1.331]
        assert {bar.source for bar in bars} == {"yahoo"}
        assert {bar.what_to_show for bar in bars} == {"TRADES"}


def test_fetch_reuses_existing_fmp_cash_instrument_and_resumes_from_yahoo_bars(monkeypatch):
    """An existing FMP-sourced USDCAD instrument must not be duplicated, and the incremental
    start date comes from the newest *yahoo* bar, not the newer FMP one."""
    session_cm = _make_session_scope()
    with session_cm() as session:
        instrument = Instrument(
            symbol="USDCAD", exchange="IDEALPRO", currency="CAD", sec_type="CASH"
        )
        session.add(instrument)
        session.flush()

        def bar(source, day):
            return PriceBar(
                instrument_id=instrument.id,
                ts=datetime(2026, 7, day, tzinfo=UTC),
                bar_size="1 day",
                source=source,
                what_to_show="TRADES",
                open=1.3,
                high=1.3,
                low=1.3,
                close=1.3,
                volume=None,
            )

        session.add_all([bar("yahoo", 2), bar("fmp", 10)])

    calls: list[tuple] = []

    def fake_download(symbol, start, end, auto_adjust, timeout):
        calls.append((symbol, start, end))
        return _frame(
            {"Open": [1.35], "High": [1.36], "Low": [1.34], "Close": [1.355], "Volume": [0.0]},
            ["2026-07-03"],
        )

    monkeypatch.setattr(yahoo_fx, "_download_within_timeout", fake_download)

    assert yahoo_fx.YahooFxConnector(session_factory=session_cm).fetch(pair="USDCAD") == 1
    assert calls[0][1] == datetime(2026, 7, 3, tzinfo=UTC).date()  # yahoo newest (Jul 2) + 1

    with session_cm() as session:
        instruments = session.scalars(select(Instrument)).all()
        assert len(instruments) == 1  # shared CASH row, no duplicate


def test_fetch_upserts_on_repeat_and_rejects_blank_pair(monkeypatch):
    session_cm = _make_session_scope()
    frames = [
        _frame(
            {"Open": [1.30], "High": [1.31], "Low": [1.29], "Close": [1.305], "Volume": [0.0]},
            ["2026-07-06"],
        ),
        _frame(
            {"Open": [1.30], "High": [1.33], "Low": [1.29], "Close": [1.320], "Volume": [0.0]},
            ["2026-07-06"],
        ),
    ]

    def fake_download(symbol, start, end, auto_adjust, timeout):
        return frames.pop(0)

    monkeypatch.setattr(yahoo_fx, "_download_within_timeout", fake_download)

    connector = yahoo_fx.YahooFxConnector(session_factory=session_cm)
    assert connector.fetch(pair="USDCAD", date_from="2026-07-06") == 1
    assert connector.fetch(pair="USDCAD", date_from="2026-07-06") == 1

    with session_cm() as session:
        bars = session.scalars(select(PriceBar)).all()
        assert len(bars) == 1  # same day upserted, not duplicated
        assert bars[0].close == 1.320

    with pytest.raises(ValueError):
        connector.fetch(pair="   ")


def test_fetch_skips_when_already_current(monkeypatch):
    """A resume date in the future must return 0 without any network call."""
    session_cm = _make_session_scope()
    with session_cm() as session:
        instrument = Instrument(
            symbol="USDCAD", exchange="IDEALPRO", currency="CAD", sec_type="CASH"
        )
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime(2999, 1, 1, tzinfo=UTC),
                bar_size="1 day",
                source="yahoo",
                what_to_show="TRADES",
                open=1.3,
                high=1.3,
                low=1.3,
                close=1.3,
                volume=None,
            )
        )

    def fail_download(*args, **kwargs):
        raise AssertionError("must not download when already current")

    monkeypatch.setattr(yahoo_fx, "_download_within_timeout", fail_download)

    assert yahoo_fx.YahooFxConnector(session_factory=session_cm).fetch(pair="USDCAD") == 0


def test_fetch_clamps_inconsistent_provider_high_low(monkeypatch):
    """Yahoo's older FX bars can report close outside [low, high]; the stored candle must be
    widened to contain open/close so the price-frame schema accepts it downstream."""
    session_cm = _make_session_scope()
    frame = _frame(
        {"Open": [1.3459], "High": [1.3581], "Low": [1.3445], "Close": [1.3721]},
        ["2004-06-25"],
    )
    frame["Volume"] = [0.0]

    monkeypatch.setattr(yahoo_fx, "_download_within_timeout", lambda *a, **k: frame)

    assert (
        yahoo_fx.YahooFxConnector(session_factory=session_cm).fetch(
            pair="USDCAD", date_from="2004-06-25"
        )
        == 1
    )
    with session_cm() as session:
        bar = session.scalars(select(PriceBar)).one()
        assert bar.high == pytest.approx(1.3721)  # widened to contain close
        assert bar.low == pytest.approx(1.3445)
        assert bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high
