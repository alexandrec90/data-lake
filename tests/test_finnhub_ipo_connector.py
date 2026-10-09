from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import IpoEvent
from data_lake.ingestion.market import finnhub_ipo as fi

# Verbatim rows from a live /calendar/ipo response (2026-10-08) — one per shape it showed.
TREX_EXPECTED = {
    "date": "2026-10-09",
    "exchange": "NASDAQ Global Select",
    "name": "TRex Bio, Inc.",
    "numberOfShares": 8333334,
    "price": "14.00-16.00",
    "status": "expected",
    "symbol": "TRXB",
    "totalSharesValue": 153333344,
}
PINE_PRICED = {
    "date": "2026-10-06",
    "exchange": "NASDAQ Global",
    "name": "Pine Tree Acquisition Corp.",
    "numberOfShares": 10000000,
    "price": "10.00",
    "status": "priced",
    "symbol": "PAXGU",
    "totalSharesValue": 100000000,
}
TANG_FILED = {
    "date": "2026-10-06",
    "exchange": None,
    "name": "TANG CAPITAL ACQUISITION CORP.",
    "numberOfShares": None,
    "price": None,
    "status": "filed",
    "symbol": "TCAA",
    "totalSharesValue": 75000000,
}
SIYATA_NO_VALUE = {
    "date": "2026-09-30",
    "exchange": "NASDAQ Capital",
    "name": "SIYATA PTT",
    "numberOfShares": 6112327,
    "price": None,
    "status": "expected",
    "symbol": "PTT",
    "totalSharesValue": 0,
}
MEEY_EMPTY_SYMBOL = {
    "date": "2026-09-11",
    "exchange": None,
    "name": "Meey Global Corp",
    "numberOfShares": None,
    "price": None,
    "status": "filed",
    "symbol": "",
    "totalSharesValue": 34500000,
}
# The same company twice in one response: a withdrawn registration and its refiling.
NEW_ICELAND_WITHDRAWN = {
    "date": "2026-10-05",
    "exchange": None,
    "name": "New Iceland Arctic Acquisition Corp.",
    "numberOfShares": None,
    "price": None,
    "status": "withdrawn",
    "symbol": None,
    "totalSharesValue": None,
}
NEW_ICELAND_FILED = {
    "date": "2026-10-05",
    "exchange": None,
    "name": "New Iceland Arctic Acquisition Corp.",
    "numberOfShares": None,
    "price": None,
    "status": "filed",
    "symbol": "NIAAU",
    "totalSharesValue": 115000000,
}


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = fi.httpx.Request("GET", "https://finnhub.io/sanitized")
            response = fi.httpx.Response(self.status_code, request=request)
            raise fi.httpx.HTTPStatusError("sanitized", request=request, response=response)

    def json(self) -> object:
        return self.payload


@pytest.fixture
def finnhub(monkeypatch):
    """Serve queued payloads, recording each request's params."""
    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")
    state = SimpleNamespace(payloads=[], calls=[])

    def fake_get(url, params, headers, timeout):
        assert url == f"{fi.BASE_URL}/calendar/ipo"
        assert headers["X-Finnhub-Token"] == "test-key"
        state.calls.append(params)
        return state.payloads.pop(0)

    monkeypatch.setattr(fi.httpx, "get", fake_get)
    return state


def _serve(state, *rows, status_code: int = 200) -> None:
    state.payloads.append(FakeResponse({"ipoCalendar": list(rows)}, status_code))


def _rows(session_cm) -> list[IpoEvent]:
    with session_cm() as session:
        return list(session.scalars(select(IpoEvent).order_by(IpoEvent.company_name)).all())


def test_fetch_maps_every_live_row_shape(finnhub):
    session_cm = make_session_scope()
    _serve(finnhub, TREX_EXPECTED, PINE_PRICED, TANG_FILED, SIYATA_NO_VALUE, MEEY_EMPTY_SYMBOL)

    count = fi.FinnhubIpoCalendarConnector(session_factory=session_cm).fetch()

    assert count == 5
    by_name = {row.company_name: row for row in _rows(session_cm)}
    trex = by_name["TRex Bio, Inc."]
    assert (trex.source, trex.stage, trex.symbol) == ("finnhub", "expected", "TRXB")
    assert trex.exchange == "NASDAQ Global Select"
    assert trex.expected_date == date(2026, 10, 9)
    assert trex.filed_at is None
    assert (trex.price_low, trex.price_high) == (14.0, 16.0)
    assert trex.shares == 8333334
    assert trex.deal_value_usd == 153333344.0
    assert trex.raw == TREX_EXPECTED  # every documented field, nothing else
    assert trex.external_id == fi.deal_key("TRex Bio, Inc.")
    assert trex.first_seen_at == trex.fetched_at

    pine = by_name["Pine Tree Acquisition Corp."]
    assert (pine.stage, pine.price_low, pine.price_high) == ("priced", 10.0, 10.0)
    assert pine.expected_date == date(2026, 10, 6)

    tang = by_name["TANG CAPITAL ACQUISITION CORP."]
    assert tang.stage == "filed"
    # SQLite drops tzinfo on readback (Postgres keeps it); compare the naive instant.
    assert tang.filed_at.replace(tzinfo=UTC) == datetime(2026, 10, 6, tzinfo=UTC)
    assert tang.expected_date is None
    assert tang.exchange is None
    assert tang.price_low is None and tang.shares is None

    assert by_name["SIYATA PTT"].deal_value_usd is None  # 0 means unknown, not a $0 deal
    assert by_name["Meey Global Corp"].symbol is None  # "" is no symbol


def test_redated_deal_updates_in_place_and_keeps_first_seen(finnhub):
    """filed -> expected moves the date and fills symbol/exchange; it is still one deal."""
    session_cm = make_session_scope()
    _serve(finnhub, MEEY_EMPTY_SYMBOL)
    fi.FinnhubIpoCalendarConnector(session_factory=session_cm).fetch()
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        row = session.scalar(select(IpoEvent))
        row.first_seen_at = marker
        row.fetched_at = marker

    scheduled = {
        **MEEY_EMPTY_SYMBOL,
        "date": "2026-10-20",
        "name": "MEEY GLOBAL CORPORATION",
        "status": "expected",
        "exchange": "NASDAQ Capital",
        "symbol": "MEEY",
        "price": "4.00-5.00",
    }
    _serve(finnhub, scheduled)
    fi.FinnhubIpoCalendarConnector(session_factory=session_cm).fetch()

    [row] = _rows(session_cm)
    assert row.stage == "expected"
    assert row.company_name == "MEEY GLOBAL CORPORATION"
    assert (row.symbol, row.exchange) == ("MEEY", "NASDAQ Capital")
    assert row.expected_date == date(2026, 10, 20)
    assert row.filed_at.replace(tzinfo=UTC) == datetime(2026, 9, 11, tzinfo=UTC)  # kept
    assert (row.price_low, row.price_high) == (4.0, 5.0)
    assert row.first_seen_at.replace(tzinfo=UTC) == marker
    assert row.fetched_at.replace(tzinfo=UTC) > marker


def test_withdrawal_keeps_what_the_deal_was_marketed_at(finnhub):
    session_cm = make_session_scope()
    _serve(finnhub, TREX_EXPECTED)
    _serve(
        finnhub,
        {
            **TREX_EXPECTED,
            "status": "withdrawn",
            "price": None,
            "exchange": None,
            "date": "2026-10-12",
        },
    )
    connector = fi.FinnhubIpoCalendarConnector(session_factory=session_cm)
    connector.fetch()
    connector.fetch()

    [row] = _rows(session_cm)
    assert row.stage == "withdrawn"
    assert (row.price_low, row.price_high) == (14.0, 16.0)
    assert row.exchange == "NASDAQ Global Select"
    assert row.expected_date == date(2026, 10, 9)  # a withdrawal's date is not a listing date
    assert row.raw["status"] == "withdrawn"
    assert row.raw["date"] == "2026-10-12"


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_duplicate_company_in_one_response_keeps_the_refiling(finnhub, order):
    session_cm = make_session_scope()
    pair = (NEW_ICELAND_WITHDRAWN, NEW_ICELAND_FILED)
    _serve(finnhub, *(pair[i] for i in order))

    assert fi.FinnhubIpoCalendarConnector(session_factory=session_cm).fetch() == 1

    [row] = _rows(session_cm)
    assert (row.stage, row.symbol, row.deal_value_usd) == ("filed", "NIAAU", 115000000.0)


def test_unstorable_rows_are_skipped(finnhub, caplog):
    session_cm = make_session_scope()
    _serve(
        finnhub,
        {**TREX_EXPECTED, "status": "postponed"},
        {**PINE_PRICED, "name": ""},
        "not-a-row",
        TANG_FILED,
        {**SIYATA_NO_VALUE, "date": "soon"},
    )

    assert fi.FinnhubIpoCalendarConnector(session_factory=session_cm).fetch() == 2
    tang, siyata = _rows(session_cm)[::-1]
    assert tang.company_name == "TANG CAPITAL ACQUISITION CORP."
    assert siyata.expected_date is None  # an unparseable date is kept in raw, not guessed
    assert siyata.raw["date"] == "soon"
    assert "postponed" in caplog.text


def test_default_and_explicit_windows(finnhub):
    session_cm = make_session_scope()
    _serve(finnhub)
    _serve(finnhub)
    connector = fi.FinnhubIpoCalendarConnector(session_factory=session_cm)

    connector.fetch()
    connector.fetch(date_from="2026-01-01", date_to="2026-03-31")

    default, explicit = finnhub.calls
    span = date.fromisoformat(default["to"]) - date.fromisoformat(default["from"])
    assert span.days == fi.DEFAULT_LOOKBACK_DAYS + fi.DEFAULT_LOOKAHEAD_DAYS
    assert explicit == {"from": "2026-01-01", "to": "2026-03-31"}


def test_bad_date_raises(finnhub):
    with pytest.raises(ValueError):
        fi.FinnhubIpoCalendarConnector().fetch(date_from="not-a-date")


def test_missing_key_raises():
    connector = fi.FinnhubIpoCalendarConnector(SimpleNamespace(finnhub_key=""))
    with pytest.raises(RuntimeError, match="FINNHUB_KEY"):
        connector.fetch()


def test_http_error_is_sanitized(finnhub):
    _serve(finnhub, status_code=403)
    with pytest.raises(fi.FinnhubProviderError) as exc_info:
        fi.FinnhubIpoCalendarConnector(session_factory=make_session_scope()).fetch()
    assert "HTTP 403" in str(exc_info.value)
    assert "test-key" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize("payload", [[], {"ipoCalendar": None}, {"error": "x"}])
def test_unexpected_shape_raises(finnhub, payload):
    finnhub.payloads.append(FakeResponse(payload))
    with pytest.raises(fi.FinnhubProviderError, match="shape"):
        fi.FinnhubIpoCalendarConnector(session_factory=make_session_scope()).fetch()


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("18.00-20.00", (18.0, 20.0)),
        ("20.00", (20.0, 20.0)),
        ("$4.00 - $5.00", (4.0, 5.0)),
        ("20.00-18.00", (18.0, 20.0)),
        ("1,000.00", (1000.0, 1000.0)),
        (12, (12.0, 12.0)),
        (None, (None, None)),
        ("", (None, None)),
        ("TBD", (None, None)),
        ("1-2-3", (None, None)),
        (True, (None, None)),
    ],
)
def test_parse_price_range(price, expected):
    assert fi.parse_price_range(price) == expected


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("TANG CAPITAL ACQUISITION CORP.", "Tang Capital Acquisition Corp"),
        ("Acme Holdings Ltd.", "ACME HOLDINGS LIMITED"),
        ("Foo Co., Ltd.", "Foo"),
    ],
)
def test_deal_key_ignores_spelling_drift(left, right):
    assert fi.deal_key(left) == fi.deal_key(right)


def test_deal_key_keeps_distinct_companies_apart():
    assert fi.deal_key("Acme Bio Inc.") != fi.deal_key("Acme Robotics Inc.")
    assert fi.normalized_name("Inc.") == "inc"  # a name that is only a suffix keeps it


@pytest.mark.parametrize(
    ("status", "stage", "filed", "expected"),
    [
        ("filed", "filed", datetime(2026, 10, 9, tzinfo=UTC), None),
        ("Expected", "expected", None, date(2026, 10, 9)),
        ("priced", "priced", None, date(2026, 10, 9)),
        ("withdrawn", "withdrawn", None, None),
    ],
)
def test_row_fields_maps_status_to_stage_and_date(status, stage, filed, expected):
    fields = fi.row_fields({**TREX_EXPECTED, "status": status})
    assert fields is not None
    assert (fields["stage"], fields["filed_at"], fields["expected_date"]) == (
        stage,
        filed,
        expected,
    )


@pytest.mark.parametrize(
    "item",
    [
        {**TREX_EXPECTED, "status": "postponed"},
        {**TREX_EXPECTED, "status": None},
        {**TREX_EXPECTED, "name": None},
        {**TREX_EXPECTED, "name": " ., "},
    ],
)
def test_row_fields_refuses_what_it_cannot_store(item):
    assert fi.row_fields(item) is None
