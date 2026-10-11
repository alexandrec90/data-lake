import logging
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import Instrument, SecFact
from data_lake.ingestion.base import AllItemsFailedError
from data_lake.ingestion.market import sec_xbrl as sx

UA = "data-lake tests test@example.com"

TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
    "3": {"cik_str": "bad", "ticker": "BAD", "title": "skipped"},
}

# Shaped like a real companyconcept response: the same period in two filings, a null fy/fp,
# frame on one fact only, a YTD 10-Q value.
PAYMENTS = {
    "cik": 320193,
    "taxonomy": "us-gaap",
    "tag": "PaymentsForRepurchaseOfCommonStock",
    "label": "Payments for Repurchase of Common Stock",
    "entityName": "Apple Inc.",
    "units": {
        "USD": [
            {
                "start": "2024-09-29",
                "end": "2025-03-29",
                "val": 48_000_000_000,
                "accn": "0000320193-25-000057",
                "fy": 2025,
                "fp": "Q2",
                "form": "10-Q",
                "filed": "2025-05-02",
            },
            {
                "start": "2024-09-29",
                "end": "2025-03-29",
                "val": 48_100_000_000,
                "accn": "0000320193-26-000010",
                "fy": None,
                "fp": None,
                "form": "10-Q",
                "filed": "2026-05-01",
                "frame": "CY2025Q1",
            },
            {"end": "2025-03-29", "val": "n/a", "accn": "x", "filed": "2025-05-02"},
            {"end": "2025-03-29", "val": 1, "filed": "2025-05-02"},  # no accession
        ]
    },
}
AUTHORIZED = {
    "taxonomy": "us-gaap",
    "units": {
        "USD": [
            {
                "end": "2025-05-01",
                "val": 100_000_000_000,
                "accn": "0000320193-25-000057",
                "fy": 2025,
                "fp": "Q2",
                "form": "10-Q",
                "filed": "2025-05-02",
            }
        ]
    },
}


class NoWait:
    def wait(self) -> None:
        pass


class FakeSec:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, object]] = {sx.TICKERS_URL: (200, TICKERS)}
        self.calls: list[str] = []

    def concept(self, cik: str, concept: str, status: int = 200, body: object = None) -> None:
        url = sx.CONCEPT_URL.format(cik=cik, taxonomy="us-gaap", concept=concept)
        self.routes[url] = (status, body if body is not None else {})

    def get(self, url, headers, timeout):
        assert headers["User-Agent"] == UA
        self.calls.append(url)
        status, body = self.routes.get(url, (404, {"error": "not found"}))
        request = httpx.Request("GET", url)
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        return httpx.Response(status, json=body, request=request)


@pytest.fixture
def sec(monkeypatch):
    monkeypatch.setattr(SETTINGS, "sec_user_agent", UA)
    fake = FakeSec()
    monkeypatch.setattr(sx.httpx, "get", fake.get)
    return fake


def _connector(session_cm) -> sx.SecFactsConnector:
    return sx.SecFactsConnector(session_factory=session_cm, pacer=NoWait())


def _facts(session_cm) -> dict[str, SecFact]:
    with session_cm() as session:
        return {row.external_id: row for row in session.scalars(select(SecFact))}


def test_parse_ticker_map_pads_ciks_and_rejects_bad_shapes():
    tickers = sx.parse_ticker_map(TICKERS)
    assert tickers["AAPL"] == "0000320193"
    assert tickers["BRK-B"] == "0001067983"
    assert "BAD" not in tickers
    with pytest.raises(sx.SecXbrlProviderError):
        sx.parse_ticker_map(["not", "a", "dict"])


def test_ticker_candidates_cover_share_class_spellings():
    assert sx.ticker_candidates("brk.b") == ["BRK.B", "BRK-B"]
    assert sx.ticker_candidates("BRK B") == ["BRK B", "BRK-B"]


@pytest.mark.parametrize(
    "row",
    [
        "junk",
        {"val": 1, "accn": "a", "filed": "2025-05-02"},  # no end
        {"end": "2025-03-29", "val": 1, "accn": "a"},  # no filing date
        {"end": "2025-03-29", "val": 1, "filed": "2025-05-02"},  # no accession
        {"end": "2025-03-29", "val": True, "accn": "a", "filed": "2025-05-02"},
        {"end": "not-a-date", "val": 1, "accn": "a", "filed": "2025-05-02"},
    ],
)
def test_fact_values_rejects_unusable_facts(row):
    assert sx.fact_values(row, cik="1", taxonomy="us-gaap", concept="C", unit="USD") is None


def test_fact_values_truncates_to_the_column_widths():
    external_id, values = sx.fact_values(
        {
            "end": "2025-03-29",
            "val": 7,
            "accn": "a",
            "filed": "2025-05-02",
            "fy": 2025,
            "fp": "FYXX",
            "form": "10-K/A-EXTRA-LONG-FORM",
            "frame": "CY2025Q1I-EXTRA-LONG",
        },
        cik="0000000001",
        taxonomy="us-gaap",
        concept="C",
        unit="USD/shares-extra-long",
    )
    assert external_id == "a:us-gaap:C:USD/shares-extra-long::2025-03-29"
    assert (values["unit"], values["fiscal_period"], values["form"], values["frame"]) == (
        "USD/shares-extra",
        "FYXX",
        "10-K/A-EXTRA-LON",
        "CY2025Q1I-EXTRA-",
    )
    assert values["value"] == 7.0 and values["fiscal_year"] == 2025


def test_parse_concept_facts_keeps_every_filing_of_a_period():
    facts = sx.parse_concept_facts(PAYMENTS, "0000320193", "PaymentsForRepurchaseOfCommonStock")

    assert set(facts) == {
        "0000320193-25-000057:us-gaap:PaymentsForRepurchaseOfCommonStock:USD:2024-09-29:2025-03-29",
        "0000320193-26-000010:us-gaap:PaymentsForRepurchaseOfCommonStock:USD:2024-09-29:2025-03-29",
    }
    restated = facts[
        "0000320193-26-000010:us-gaap:PaymentsForRepurchaseOfCommonStock:USD:2024-09-29:2025-03-29"
    ]
    assert (restated["fiscal_year"], restated["fiscal_period"], restated["frame"]) == (
        None,
        None,
        "CY2025Q1",
    )
    instant = sx.parse_concept_facts(AUTHORIZED, "0000320193", "X")
    [(key, values)] = instant.items()
    assert key.endswith(":USD::2025-05-01")
    assert values["period_start"] is None
    with pytest.raises(sx.SecXbrlProviderError):
        sx.parse_concept_facts({"units": []}, "0000320193", "X")


def test_fetch_stores_facts_links_instruments_and_skips_404s_and_unknown_tickers(sec, caplog):
    sec.concept("0000320193", "PaymentsForRepurchaseOfCommonStock", body=PAYMENTS)
    sec.concept("0000320193", "StockRepurchaseProgramAuthorizedAmount1", body=AUTHORIZED)
    # StockRepurchasedDuringPeriodShares is unrouted -> 404: never reported, not an error.
    # BRK.B resolves to BRK-B's CIK; every concept 404s.
    session_cm = make_session_scope()
    with session_cm() as session:
        session.add(Instrument(symbol="AAPL", exchange="SMART", currency="USD"))

    with caplog.at_level(logging.INFO, logger=sx.__name__):
        count = _connector(session_cm).fetch(["aapl", "BRK.B", "SHOP"])

    assert count == 3
    assert "SHOP" in caplog.text
    assert sum("CIK0001067983" in url for url in sec.calls) == 3
    assert sum(url == sx.TICKERS_URL for url in sec.calls) == 1
    facts = _facts(session_cm)
    with session_cm() as session:
        aapl_id = session.scalar(select(Instrument.id).where(Instrument.symbol == "AAPL"))
    assert {fact.instrument_id for fact in facts.values()} == {aapl_id}
    first = facts[
        "0000320193-25-000057:us-gaap:PaymentsForRepurchaseOfCommonStock:USD:2024-09-29:2025-03-29"
    ]
    assert (first.source, first.cik, first.taxonomy, first.unit) == (
        "sec_xbrl",
        "0000320193",
        "us-gaap",
        "USD",
    )
    assert (first.period_start, first.period_end, first.filed) == (
        date(2024, 9, 29),
        date(2025, 3, 29),
        date(2025, 5, 2),
    )
    assert (first.value, first.fiscal_year, first.fiscal_period, first.form) == (
        48e9,
        2025,
        "Q2",
        "10-Q",
    )


def test_refetch_updates_in_place_and_keeps_first_seen(sec):
    sec.concept("0000320193", "PaymentsForRepurchaseOfCommonStock", body=PAYMENTS)
    session_cm = make_session_scope()
    _connector(session_cm).fetch(["AAPL"], concepts=["PaymentsForRepurchaseOfCommonStock"])
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        for row in session.scalars(select(SecFact)):
            row.first_seen_at = marker
        session.add(Instrument(symbol="AAPL", exchange="SMART", currency="USD"))

    assert (
        _connector(session_cm).fetch(["AAPL"], concepts=["PaymentsForRepurchaseOfCommonStock"]) == 2
    )

    facts = _facts(session_cm)
    assert len(facts) == 2
    for fact in facts.values():
        assert fact.first_seen_at.replace(tzinfo=UTC) == marker
        assert fact.fetched_at.replace(tzinfo=UTC) > marker
        assert fact.instrument_id is not None  # linked once the instrument exists


def test_every_request_failing_raises(sec):
    for concept in sx.DEFAULT_CONCEPTS:
        sec.concept("0000320193", concept, status=500)

    with pytest.raises(AllItemsFailedError, match="HTTP 500"):
        _connector(make_session_scope()).fetch(["AAPL"])


def test_some_requests_failing_is_tolerated(sec):
    sec.concept("0000320193", "PaymentsForRepurchaseOfCommonStock", body=PAYMENTS)
    sec.concept("0000320193", "StockRepurchaseProgramAuthorizedAmount1", status=503)
    sec.concept("0000320193", "StockRepurchasedDuringPeriodShares", status=200, body="<html>")

    assert _connector(make_session_scope()).fetch(["AAPL"]) == 2


def test_only_unknown_tickers_returns_zero(sec):
    assert _connector(make_session_scope()).fetch(["SHOP"]) == 0
    assert sec.calls == [sx.TICKERS_URL]


def test_ticker_map_failures_and_bad_arguments(sec, monkeypatch):
    with pytest.raises(ValueError, match="symbols"):
        _connector(make_session_scope()).fetch([])
    with pytest.raises(ValueError, match="concepts"):
        _connector(make_session_scope()).fetch(["AAPL"], concepts=[])

    sec.routes[sx.TICKERS_URL] = (403, "Forbidden")
    with pytest.raises(sx.SecXbrlProviderError, match="SEC_USER_AGENT") as exc_info:
        _connector(make_session_scope()).fetch(["AAPL"])
    assert UA not in str(exc_info.value)

    sec.routes[sx.TICKERS_URL] = (200, "not json")
    with pytest.raises(sx.SecXbrlProviderError, match="not JSON"):
        _connector(make_session_scope()).fetch(["AAPL"])

    def broken(url, headers, timeout):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(sx.httpx, "get", broken)
    with pytest.raises(sx.SecXbrlProviderError, match="ConnectError"):
        _connector(make_session_scope()).fetch(["AAPL"])

    monkeypatch.setattr(SETTINGS, "sec_user_agent", " ")
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        _connector(make_session_scope()).fetch(["AAPL"])
