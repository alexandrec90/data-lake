import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import Instrument, PriceBar
from data_lake.ingestion.market import index_membership, tiingo

KEY = "sekrit-tiingo-key-123"


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _response(status: int, payload=None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.tiingo.com/tiingo/daily/x/prices")
    return httpx.Response(status, json=payload, request=request)


def _row(day: str, adj_close: float | None, **extra) -> dict:
    row = {
        "date": f"{day}T00:00:00.000Z",
        "open": 99.0,
        "high": 99.0,
        "low": 99.0,
        "close": 99.0,
        "volume": 1,
        "adjOpen": 10.0,
        "adjHigh": 12.0,
        "adjLow": 9.0,
        "adjClose": adj_close,
        "adjVolume": 500,
        "divCash": 0.0,
        "splitFactor": 1.0,
    }
    row.update(extra)
    return row


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(SETTINGS, "tiingo_api_key", KEY)


# --- supported tickers --------------------------------------------------------------------


def _zip(csv_text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("supported_tickers.csv", csv_text)
    return buffer.getvalue()


def test_parse_supported_tickers_indexes_by_ticker_and_keeps_reused_rows():
    listings = tiingo.parse_supported_tickers(
        _zip(
            "ticker,exchange,assetType,priceCurrency,startDate,endDate\n"
            "do,NYSE,Stock,USD,1995-10-11,2022-11-30\n"
            "DO,NYSE,Stock,USD,2022-12-01,2024-09-03\n"
            "SIVBQ,PINK,Stock,USD,1987-03-26,\n"
            "NODATE,NYSE,Stock,USD,,\n"
        )
    )
    assert set(listings) == {"DO", "SIVBQ"}
    assert len(listings["DO"]) == 2
    assert listings["SIVBQ"] == [
        tiingo.TiingoListing("SIVBQ", "PINK", "Stock", "USD", date(1987, 3, 26), None)
    ]


def test_parse_supported_tickers_rejects_a_zip_without_csv():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "nope")
    with pytest.raises(ValueError, match="no CSV"):
        tiingo.parse_supported_tickers(buffer.getvalue())


def test_fetch_supported_tickers_downloads_the_public_zip(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, content=b"zip", request=httpx.Request("GET", url))

    monkeypatch.setattr(tiingo.httpx, "get", fake_get)
    assert tiingo.fetch_supported_tickers() == b"zip"
    assert calls == [tiingo.SUPPORTED_TICKERS_URL]


def test_membership_http_get_text_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        index_membership.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(404, request=httpx.Request("GET", url)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        index_membership._http_get_text("https://example.invalid/x.csv")


# --- usage ledger -------------------------------------------------------------------------


def test_usage_counts_each_symbol_once_per_month_and_rolls_over():
    clock = _Clock(datetime(2026, 10, 30, 12, tzinfo=UTC))
    usage = tiingo.TiingoUsage(monthly_symbols=2, hourly_requests=100, clock=clock)
    usage.record("aaa")
    usage.record("BBB")
    usage.check("AAA")  # already counted this month: no new slot needed
    with pytest.raises(tiingo.TiingoBudgetExhausted, match="monthly"):
        usage.check("CCC")

    clock.now = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    usage.check("CCC")


def test_usage_hourly_and_daily_windows_roll():
    clock = _Clock(datetime(2026, 10, 6, 10, 0, tzinfo=UTC))
    usage = tiingo.TiingoUsage(hourly_requests=2, daily_requests=3, clock=clock)
    usage.record("A")
    clock.now += timedelta(minutes=50)
    usage.record("A")
    with pytest.raises(tiingo.TiingoBudgetExhausted, match="hourly"):
        usage.check("A")

    clock.now += timedelta(minutes=15)  # first request is now more than an hour old
    usage.check("A")
    usage.record("A")
    clock.now += timedelta(hours=2)
    with pytest.raises(tiingo.TiingoBudgetExhausted, match="daily"):
        usage.check("A")

    clock.now += timedelta(hours=22)
    usage.check("A")


def test_usage_persists_to_json_and_reloads(tmp_path):
    path = tmp_path / "logs" / "tiingo-usage.json"
    clock = _Clock(datetime(2026, 10, 6, 10, tzinfo=UTC))
    first = tiingo.TiingoUsage(path, monthly_symbols=1, clock=clock)
    first.record("AAA")
    assert json.loads(path.read_text(encoding="utf-8"))["symbols"] == ["AAA"]

    second = tiingo.TiingoUsage(path, monthly_symbols=1, clock=clock)
    with pytest.raises(tiingo.TiingoBudgetExhausted):
        second.check("BBB")


def test_usage_refuses_a_corrupt_ledger(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable Tiingo usage ledger"):
        tiingo.TiingoUsage(path)


def test_usage_defaults_sit_under_the_free_tier():
    usage = tiingo.TiingoUsage()
    assert usage.monthly_symbols < 500
    assert usage.hourly_requests < 50
    assert usage.daily_requests < 1000


# --- connector ----------------------------------------------------------------------------


def test_fetch_stores_adjusted_fields_under_the_listing_exchange(monkeypatch, keyed):
    scope = make_session_scope()
    calls = []

    def fake_get(ticker, params, token):
        calls.append((ticker, params, token))
        return _response(
            200,
            [
                _row("2023-03-08", 267.83),
                _row("2023-03-09", 106.04, adjOpen=None, adjVolume=None),
                _row("2023-03-10", None),  # no adjusted close: dropped
            ],
        )

    monkeypatch.setattr(tiingo, "_get_prices", fake_get)
    usage = tiingo.TiingoUsage(monthly_symbols=1)
    connector = tiingo.TiingoConnector(session_factory=scope, usage=usage)

    count = connector.fetch(
        symbol="sivbq", date_from="2022-01-01", date_to="2023-03-31", exchange="pink"
    )

    assert count == 2
    assert calls == [
        ("SIVBQ", {"startDate": "2022-01-01", "format": "json", "endDate": "2023-03-31"}, KEY)
    ]
    with scope() as session:
        instrument = session.scalar(select(Instrument))
        assert instrument is not None
        assert (instrument.symbol, instrument.exchange, instrument.currency) == (
            "SIVBQ",
            "PINK",
            "USD",
        )
        bars = session.scalars(select(PriceBar).order_by(PriceBar.ts)).all()
        assert [(b.ts.date(), b.open, b.high, b.low, b.close, b.volume) for b in bars] == [
            (date(2023, 3, 8), 10.0, 12.0, 9.0, 267.83, 500.0),
            (date(2023, 3, 9), 106.04, 12.0, 9.0, 106.04, None),
        ]
        assert {(b.source, b.what_to_show, b.bar_size) for b in bars} == {
            ("tiingo", "ADJUSTED_LAST", "1 day")
        }
    # the request was counted: the one monthly slot is now SIVBQ's
    usage.check("SIVBQ")
    with pytest.raises(tiingo.TiingoBudgetExhausted):
        usage.check("OTHER")


def test_fetch_resumes_after_the_last_stored_bar(monkeypatch, keyed):
    scope = make_session_scope()
    starts = []

    def fake_get(ticker, params, token):
        starts.append(params["startDate"])
        return _response(200, [_row("2024-01-02", 5.0)])

    monkeypatch.setattr(tiingo, "_get_prices", fake_get)
    connector = tiingo.TiingoConnector(session_factory=scope)
    connector.fetch(symbol="AAA")
    connector.fetch(symbol="AAA")
    assert starts == [tiingo.EARLIEST_DATE.isoformat(), "2024-01-03"]
    with scope() as session:
        assert session.scalar(select(Instrument.exchange)) == "TIINGO"


def test_fetch_skips_an_empty_range_without_a_request(monkeypatch, keyed):
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: pytest.fail("no request expected"))
    connector = tiingo.TiingoConnector(session_factory=make_session_scope())
    assert connector.fetch(symbol="A", date_from="2024-02-01", date_to="2024-01-01") == 0


def test_fetch_requires_the_key_and_a_symbol(keyed, monkeypatch):
    connector = tiingo.TiingoConnector(session_factory=make_session_scope())
    with pytest.raises(ValueError, match="symbol"):
        connector.fetch(symbol=" ")
    monkeypatch.setattr(SETTINGS, "tiingo_api_key", "")
    with pytest.raises(RuntimeError, match="TIINGO_API_KEY"):
        connector.fetch(symbol="AAA")


def test_fetch_404_returns_zero_and_creates_no_instrument(monkeypatch, keyed):
    scope = make_session_scope()
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: _response(404, {"detail": "nope"}))
    assert tiingo.TiingoConnector(session_factory=scope).fetch(symbol="GONE") == 0
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: _response(200, []))
    assert tiingo.TiingoConnector(session_factory=scope).fetch(symbol="EMPTY") == 0
    with scope() as session:
        assert session.scalars(select(Instrument)).all() == []


@pytest.mark.parametrize("status", [401, 403])
def test_fetch_auth_failure_never_leaks_the_key(monkeypatch, keyed, status):
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: _response(status, {"detail": KEY}))
    with pytest.raises(tiingo.TiingoProviderError) as exc_info:
        tiingo.TiingoConnector(session_factory=make_session_scope()).fetch(symbol="AAA")
    assert exc_info.value.status == status
    assert KEY not in str(exc_info.value)
    assert "TIINGO_API_KEY" in str(exc_info.value)


def test_fetch_429_is_budget_exhaustion(monkeypatch, keyed):
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: _response(429))
    with pytest.raises(tiingo.TiingoBudgetExhausted):
        tiingo.TiingoConnector(session_factory=make_session_scope()).fetch(symbol="AAA")


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (_response(500), "HTTP 500"),
        (_response(200, {"not": "a list"}), "unexpected"),
    ],
)
def test_fetch_other_failures_are_provider_errors(monkeypatch, keyed, response, match):
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: response)
    with pytest.raises(tiingo.TiingoProviderError, match=match):
        tiingo.TiingoConnector(session_factory=make_session_scope()).fetch(symbol="AAA")


def test_fetch_network_error_is_a_provider_error_without_the_key(monkeypatch, keyed):
    def boom(ticker, params, token):
        raise httpx.ConnectError(f"failed with token {token}")

    monkeypatch.setattr(tiingo, "_get_prices", boom)
    with pytest.raises(tiingo.TiingoProviderError) as exc_info:
        tiingo.TiingoConnector(session_factory=make_session_scope()).fetch(symbol="AAA")
    assert KEY not in str(exc_info.value)


def test_fetch_checks_the_ledger_before_any_request(monkeypatch, keyed):
    monkeypatch.setattr(tiingo, "_get_prices", lambda *a: pytest.fail("no request expected"))
    usage = tiingo.TiingoUsage(monthly_symbols=0)
    with pytest.raises(tiingo.TiingoBudgetExhausted):
        tiingo.TiingoConnector(session_factory=make_session_scope(), usage=usage).fetch(
            symbol="AAA"
        )


def test_get_prices_sends_the_key_as_a_header_not_a_param(monkeypatch):
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(url=url, params=params, headers=headers)
        return _response(200, [])

    monkeypatch.setattr(tiingo.httpx, "get", fake_get)
    tiingo._get_prices("AAA", {"startDate": "2024-01-01"}, KEY)
    assert seen["url"] == "https://api.tiingo.com/tiingo/daily/AAA/prices"
    assert seen["headers"]["Authorization"] == f"Token {KEY}"
    assert KEY not in json.dumps(seen["params"])
