from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from _lake_env import SETTINGS
from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market import alpha_vantage as av


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
        request = av.httpx.Request("GET", "https://www.alphavantage.co/sanitized")
        response = av.httpx.Response(self.status_code, request=request)
        raise av.httpx.HTTPStatusError("sanitized", request=request, response=response)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    # the premium-refusal memo is process-wide on purpose; isolate it per test
    av._adjusted_supported = None
    yield
    av._adjusted_supported = None


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


def _adjusted_payload() -> dict:
    return {
        "Meta Data": {"2. Symbol": "NVDA"},
        av.SERIES_KEY: {
            "2026-07-14": {
                "1. open": "100.0",
                "2. high": "110.0",
                "3. low": "90.0",
                "4. close": "100.0",
                "5. adjusted close": "50.0",
                "6. volume": "1000",
                "7. dividend amount": "0.0",
                "8. split coefficient": "2.0",
            },
            "2026-07-15": {
                "1. open": "51.0",
                "2. high": "52.0",
                "3. low": "50.0",
                "4. close": "51.5",
                "5. adjusted close": "51.5",
                "6. volume": "2000",
                "7. dividend amount": "0.0",
                "8. split coefficient": "1.0",
            },
        },
    }


def _daily_payload() -> dict:
    return {
        "Meta Data": {"2. Symbol": "NVDA"},
        av.SERIES_KEY: {
            "2026-07-15": {
                "1. open": "51.0",
                "2. high": "52.0",
                "3. low": "50.0",
                "4. close": "51.5",
                "5. volume": "2000",
            },
        },
    }


PREMIUM_REFUSAL = {
    "Information": "Thank you for using Alpha Vantage! This is a premium endpoint. Please "
    "subscribe to a premium membership plan to instantly unlock this endpoint."
}


def test_alpha_vantage_upserts_adjusted_bars(monkeypatch):
    session_cm = _make_session_scope()

    def fake_get(url, params, timeout):
        assert url == av.BASE_URL
        assert params["function"] == "TIME_SERIES_DAILY_ADJUSTED"
        assert params["symbol"] == "NVDA"
        assert params["apikey"] == "test-key"
        assert timeout == 30
        return FakeResponse(_adjusted_payload())

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    count = av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="nvda")

    assert count == 2
    with session_cm() as session:
        bars = session.scalars(select(PriceBar).order_by(PriceBar.ts)).all()
        assert len(bars) == 2
        first = bars[0]
        assert first.source == "alpha_vantage"
        assert first.bar_size == "1 day"
        assert first.what_to_show == "ADJUSTED_LAST"
        # split day: adjusted close 50 on raw close 100 → OHL back-adjusted by 0.5
        assert first.close == pytest.approx(50.0)
        assert first.open == pytest.approx(50.0)
        assert first.high == pytest.approx(55.0)
        assert first.low == pytest.approx(45.0)
        assert first.volume == pytest.approx(1000.0)
        assert first.ts.replace(tzinfo=UTC) == datetime(2026, 7, 14, tzinfo=UTC)

        instrument = session.scalar(select(Instrument))
        assert (instrument.symbol, instrument.exchange, instrument.currency) == (
            "NVDA",
            "SMART",
            "USD",
        )


def test_alpha_vantage_upsert_is_idempotent(monkeypatch):
    session_cm = _make_session_scope()

    def fake_get(url, params, timeout):
        return FakeResponse(_adjusted_payload())

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    assert av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="NVDA") == 2
    assert av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="NVDA") == 2

    with session_cm() as session:
        assert len(session.scalars(select(PriceBar)).all()) == 2  # upsert, not duplicate


def test_alpha_vantage_falls_back_to_unadjusted_on_premium_refusal(monkeypatch):
    session_cm = _make_session_scope()
    calls: list[str] = []

    def fake_get(url, params, timeout):
        calls.append(params["function"])
        if params["function"] == "TIME_SERIES_DAILY_ADJUSTED":
            return FakeResponse(PREMIUM_REFUSAL)
        return FakeResponse(_daily_payload())

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    count = av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="NVDA")

    assert calls == ["TIME_SERIES_DAILY_ADJUSTED", "TIME_SERIES_DAILY"]
    assert count == 1
    with session_cm() as session:
        bar = session.scalar(select(PriceBar))
        assert bar.what_to_show == "TRADES"  # unadjusted fallback
        assert bar.close == pytest.approx(51.5)
        assert bar.volume == pytest.approx(2000.0)


def test_alpha_vantage_trt_symbol_maps_to_tsx(monkeypatch):
    session_cm = _make_session_scope()

    def fake_get(url, params, timeout):
        assert params["symbol"] == "SHOP.TRT"
        return FakeResponse(_adjusted_payload())

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="shop.trt")

    with session_cm() as session:
        instrument = session.scalar(select(Instrument))
        assert (instrument.symbol, instrument.exchange, instrument.currency) == (
            "SHOP",
            "TSX",
            "CAD",
        )


def test_alpha_vantage_rate_limit_raises_quota_error(monkeypatch):
    def fake_get(url, params, timeout):
        if params["function"] == "TIME_SERIES_DAILY_ADJUSTED":
            return FakeResponse(PREMIUM_REFUSAL)
        return FakeResponse(
            {"Information": "We have detected your API key and daily rate limit of 25 requests"}
        )

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    with pytest.raises(av.AlphaVantageQuotaError, match="rate limit"):
        av.AlphaVantageConnector().fetch(symbol="NVDA")


def test_alpha_vantage_quota_message_wins_over_premium_fallback(monkeypatch):
    """The daily-cap body also advertises premium plans — it must abort, not fall back."""
    calls: list[str] = []

    def fake_get(url, params, timeout):
        calls.append(params["function"])
        return FakeResponse(
            {
                "Information": "our standard API rate limit is 25 requests per day. "
                "Please subscribe to any of the premium plans"
            }
        )

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    with pytest.raises(av.AlphaVantageQuotaError):
        av.AlphaVantageConnector().fetch(symbol="NVDA")
    assert calls == ["TIME_SERIES_DAILY_ADJUSTED"]  # no second request wasted


def test_alpha_vantage_remembers_premium_refusal_across_fetches(monkeypatch):
    """After one premium refusal, later fetches skip the doomed adjusted probe entirely."""
    session_cm = _make_session_scope()
    calls: list[str] = []

    def fake_get(url, params, timeout):
        calls.append(params["function"])
        if params["function"] == "TIME_SERIES_DAILY_ADJUSTED":
            return FakeResponse(PREMIUM_REFUSAL)
        return FakeResponse(_daily_payload())

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="NVDA")
    av.AlphaVantageConnector(session_factory=session_cm).fetch(symbol="MSFT")

    assert calls == [
        "TIME_SERIES_DAILY_ADJUSTED",  # probe once
        "TIME_SERIES_DAILY",
        "TIME_SERIES_DAILY",  # second symbol goes straight to the free endpoint
    ]


def test_alpha_vantage_error_message_raises(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse({"Error Message": "Invalid API call for symbol WAT"})

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    with pytest.raises(av.AlphaVantageProviderError, match="Invalid API call"):
        av.AlphaVantageConnector().fetch(symbol="WAT")


def test_alpha_vantage_missing_key_raises():
    connector = av.AlphaVantageConnector(SimpleNamespace(alpha_vantage_key=""))
    with pytest.raises(RuntimeError, match="ALPHA_VANTAGE_KEY"):
        connector.fetch(symbol="NVDA")


def test_alpha_vantage_missing_symbol_raises(monkeypatch):
    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    with pytest.raises(ValueError, match="symbol"):
        av.AlphaVantageConnector().fetch(symbol="  ")


def test_alpha_vantage_sanitizes_http_errors(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeHttpErrorResponse({}, status_code=503)

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    with pytest.raises(av.AlphaVantageProviderError) as exc_info:
        av.AlphaVantageConnector().fetch(symbol="NVDA")
    assert "HTTP 503" in str(exc_info.value)
    assert "API key omitted" in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_alpha_vantage_unexpected_shape_raises(monkeypatch):
    def fake_get(url, params, timeout):
        return FakeResponse({"Meta Data": {}})

    monkeypatch.setattr(SETTINGS, "alpha_vantage_key", "test-key")
    monkeypatch.setattr(av.httpx, "get", fake_get)

    with pytest.raises(av.AlphaVantageProviderError, match="response shape"):
        av.AlphaVantageConnector().fetch(symbol="NVDA")


# --- batch mode (fetch_universe / read_tickers_file) -----------------------------------


def test_read_tickers_file_skips_comments_and_dedupes(tmp_path):
    tickers = tmp_path / "t.txt"
    tickers.write_text("# comment\n\nnvda\nMSFT\nnvda\n shop.trt \n", encoding="utf-8")
    assert av.read_tickers_file(str(tickers)) == ["NVDA", "MSFT", "SHOP.TRT"]


def _seed_fresh_bar(session_cm, symbol: str, age_days: float) -> None:
    from datetime import timedelta

    with session_cm() as session:
        instrument = Instrument(symbol=symbol, exchange="SMART", currency="USD")
        session.add(instrument)
        session.flush()
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime.now(UTC) - timedelta(days=age_days),
                bar_size="1 day",
                source="alpha_vantage",
                what_to_show="TRADES",
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
            )
        )


def test_fetch_universe_skips_fresh_symbols(monkeypatch):
    session_cm = _make_session_scope()
    _seed_fresh_bar(session_cm, "NVDA", age_days=0.1)  # fetched moments ago
    fetched: list[str] = []

    monkeypatch.setattr(
        av.AlphaVantageConnector, "fetch", lambda self, symbol="", **kw: fetched.append(symbol) or 3
    )

    total = av.fetch_universe(["NVDA", "MSFT"], sleep=lambda _s: None, session_factory=session_cm)

    assert fetched == ["MSFT"]  # NVDA fresh — no request spent
    assert total == 3


def test_fetch_universe_stale_symbol_is_refetched(monkeypatch):
    session_cm = _make_session_scope()
    _seed_fresh_bar(session_cm, "NVDA", age_days=5.0)  # older than the 1-day default
    fetched: list[str] = []

    monkeypatch.setattr(
        av.AlphaVantageConnector, "fetch", lambda self, symbol="", **kw: fetched.append(symbol) or 1
    )

    av.fetch_universe(["NVDA"], sleep=lambda _s: None, session_factory=session_cm)
    assert fetched == ["NVDA"]


def test_fetch_universe_respects_request_budget(monkeypatch):
    session_cm = _make_session_scope()
    fetched: list[str] = []

    monkeypatch.setattr(
        av.AlphaVantageConnector, "fetch", lambda self, symbol="", **kw: fetched.append(symbol) or 1
    )

    total = av.fetch_universe(
        ["A", "B", "C", "D"], max_requests=2, sleep=lambda _s: None, session_factory=session_cm
    )

    assert fetched == ["A", "B"]  # budget stops the run; C/D stay stale for the next run
    assert total == 2


def test_fetch_universe_aborts_on_quota_error(monkeypatch):
    session_cm = _make_session_scope()
    fetched: list[str] = []

    def fake_fetch(self, symbol="", **kw):
        fetched.append(symbol)
        if symbol == "B":
            raise av.AlphaVantageQuotaError("quota exhausted")
        return 1

    monkeypatch.setattr(av.AlphaVantageConnector, "fetch", fake_fetch)

    total = av.fetch_universe(["A", "B", "C"], sleep=lambda _s: None, session_factory=session_cm)

    assert fetched == ["A", "B"]  # C not attempted — it could only fail too
    assert total == 1


def test_fetch_universe_skips_a_failing_symbol(monkeypatch):
    session_cm = _make_session_scope()

    def fake_fetch(self, symbol="", **kw):
        if symbol == "BAD":
            raise av.AlphaVantageProviderError("Invalid API call")
        return 1

    monkeypatch.setattr(av.AlphaVantageConnector, "fetch", fake_fetch)

    assert (
        av.fetch_universe(["A", "BAD", "C"], sleep=lambda _s: None, session_factory=session_cm) == 2
    )


def test_fetch_universe_spaces_calls(monkeypatch):
    session_cm = _make_session_scope()
    sleeps: list[float] = []

    monkeypatch.setattr(av.AlphaVantageConnector, "fetch", lambda self, **kw: 1)

    av.fetch_universe(
        ["A", "B", "C"], spacing_seconds=1.5, sleep=sleeps.append, session_factory=session_cm
    )
    assert sleeps == [1.5, 1.5]  # between calls, not before the first
