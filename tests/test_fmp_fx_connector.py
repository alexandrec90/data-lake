from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from _lake_env import SETTINGS
from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market import fmp, fmp_fx


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


def _make_session_scope():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


def test_fx_fetch_creates_cash_instrument_and_bars(monkeypatch):
    payload = [
        {"date": "2024-01-02", "open": 1.33, "high": 1.34, "low": 1.32, "close": 1.335},
        {"date": "2024-01-03", "open": 1.335, "high": 1.34, "low": 1.33, "close": 1.331},
    ]
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        assert params["symbol"] == "USDCAD"
        assert "apikey" not in params
        return FakeResponse(payload)

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    count = fmp_fx.FmpFxConnector(session_factory=session_cm).fetch(pair="usdcad")

    assert count == 2
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "USDCAD"))
        assert instrument is not None
        assert instrument.sec_type == "CASH"
        assert instrument.currency == "CAD"  # quote currency of USDCAD
        bars = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == instrument.id).order_by(PriceBar.ts)
        ).all()
        assert [round(bar.close, 3) for bar in bars] == [1.335, 1.331]
        assert bars[0].source == "fmp"


def test_fx_fetch_upserts_on_repeat(monkeypatch):
    payloads = [
        [{"date": "2024-01-02", "open": 1.33, "high": 1.34, "low": 1.32, "close": 1.335}],
        [{"date": "2024-01-02", "open": 1.33, "high": 1.36, "low": 1.32, "close": 1.350}],
    ]
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    assert fmp_fx.FmpFxConnector(session_factory=session_cm).fetch(pair="USDCAD") == 1
    assert fmp_fx.FmpFxConnector(session_factory=session_cm).fetch(pair="USDCAD") == 1

    with session_cm() as session:
        bars = session.scalars(select(PriceBar)).all()
        assert len(bars) == 1  # same day upserted, not duplicated
        assert bars[0].close == 1.350
