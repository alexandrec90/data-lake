from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from _lake_env import SETTINGS
from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market import fmp


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeHttpErrorResponse(FakeResponse):
    def raise_for_status(self) -> None:
        request = fmp.httpx.Request("GET", "https://financialmodelingprep.com/sanitized")
        response = fmp.httpx.Response(self.status_code, request=request)
        raise fmp.httpx.HTTPStatusError(
            "sanitized provider error", request=request, response=response
        )


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


def test_fmp_fetch_upserts_price_bars(monkeypatch):
    payload = [
        {"date": "2024-01-03", "open": 189.0, "high": 191.0, "low": 188.5, "close": 190.2},
        {
            "date": "2024-01-02",
            "open": 187.1,
            "high": 189.4,
            "low": 186.8,
            "close": 188.9,
            "volume": 1000,
        },
    ]
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        assert url == f"{fmp.BASE_URL}{fmp.FULL_EOD_PATH}"
        assert params["symbol"] == "AAPL"
        assert "from" not in params
        assert "apikey" not in params
        assert headers["apikey"] == "test-fmp-key"  # pragma: allowlist secret
        assert timeout == 30
        return FakeResponse(payload)

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    count = fmp.FmpConnector(session_factory=session_cm).fetch(symbol="aapl")

    assert count == 2
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "AAPL"))
        assert instrument is not None
        bars = session.scalars(
            select(PriceBar).where(PriceBar.instrument_id == instrument.id).order_by(PriceBar.ts)
        ).all()
        assert len(bars) == 2
        assert bars[0].source == "fmp"
        assert bars[0].bar_size == "1 day"
        assert bars[0].open == 187.1
        assert bars[0].volume == 1000.0
        assert bars[1].volume is None


def test_fmp_fetch_updates_existing_price_bar(monkeypatch):
    first_payload = [
        {"date": "2024-01-02", "open": 187.1, "high": 189.4, "low": 186.8, "close": 188.9}
    ]
    second_payload = [
        {"date": "2024-01-02", "open": 187.1, "high": 190.0, "low": 186.8, "close": 189.5}
    ]
    payloads = [first_payload, second_payload]
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    assert fmp.FmpConnector(session_factory=session_cm).fetch(symbol="AAPL") == 1
    assert (
        fmp.FmpConnector(session_factory=session_cm).fetch(symbol="AAPL", date_from="2024-01-02")
        == 1
    )

    with session_cm() as session:
        bars = session.scalars(select(PriceBar)).all()
        assert len(bars) == 1
        assert bars[0].high == 190.0
        assert bars[0].close == 189.5


def test_fmp_fetch_starts_from_next_missing_date(monkeypatch):
    first_payload = [
        {"date": "2024-01-02", "open": 187.1, "high": 189.4, "low": 186.8, "close": 188.9}
    ]
    second_payload = [
        {"date": "2024-01-03", "open": 189.0, "high": 191.0, "low": 188.5, "close": 190.2}
    ]
    payloads = [first_payload, second_payload]
    params_seen: list[dict[str, str]] = []
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        params_seen.append(dict(params))
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    assert fmp.FmpConnector(session_factory=session_cm).fetch(symbol="AAPL") == 1
    assert fmp.FmpConnector(session_factory=session_cm).fetch(symbol="AAPL") == 1

    assert "from" not in params_seen[0]
    assert params_seen[1]["from"] == "2024-01-03"
    with session_cm() as session:
        assert len(session.scalars(select(PriceBar)).all()) == 2


def test_fmp_fetch_skips_request_when_incremental_range_is_after_date_to(monkeypatch):
    session_cm = _make_session_scope()

    def fail_get(url, params, headers, timeout):
        raise AssertionError("FMP should not be called when local data already covers date_to")

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fail_get)

    with session_cm() as session:
        instrument = Instrument(symbol="AAPL", exchange="SMART", currency="USD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=fmp._daily_ts("2024-01-02"),
                bar_size="1 day",
                source="fmp",
                what_to_show="TRADES",
                open=187.1,
                high=189.4,
                low=186.8,
                close=188.9,
                volume=1000,
            )
        )

    assert (
        fmp.FmpConnector(session_factory=session_cm).fetch(symbol="AAPL", date_to="2024-01-02") == 0
    )


def test_fmp_fetch_falls_back_to_light_eod_when_full_is_gated(monkeypatch):
    payload = [{"date": "2024-01-02", "price": 188.9, "volume": 1000}]
    urls: list[str] = []
    session_cm = _make_session_scope()

    class GatedResponse(FakeResponse):
        def raise_for_status(self) -> None:
            return None

    def fake_get(url, params, headers, timeout):
        urls.append(url)
        if url.endswith(fmp.FULL_EOD_PATH):
            return GatedResponse({"error": "payment required"}, status_code=402)
        return FakeResponse(payload)

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    assert fmp.FmpConnector(session_factory=session_cm).fetch(symbol="GOOG") == 1
    assert urls == [f"{fmp.BASE_URL}{fmp.FULL_EOD_PATH}", f"{fmp.BASE_URL}{fmp.LIGHT_EOD_PATH}"]

    with session_cm() as session:
        bar = session.scalar(select(PriceBar))
        assert bar is not None
        assert bar.open == 188.9
        assert bar.high == 188.9
        assert bar.low == 188.9
        assert bar.close == 188.9


def test_fmp_fetch_sanitizes_provider_http_errors(monkeypatch):
    session_cm = _make_session_scope()

    def fake_get(url, params, headers, timeout):
        return FakeHttpErrorResponse({"error": "payment required"}, status_code=402)

    monkeypatch.setattr(SETTINGS, "fmp_key", "test-fmp-key")
    monkeypatch.setattr(fmp.httpx, "get", fake_get)

    with pytest.raises(fmp.FmpProviderError) as exc_info:
        fmp.FmpConnector(session_factory=session_cm).fetch(symbol="GOOG")

    assert "HTTP 402" in str(exc_info.value)
    assert "API key omitted" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
