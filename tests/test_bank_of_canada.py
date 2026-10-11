from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from data_lake.db.models import EconomicEvent, MacroObservation
from data_lake.ingestion.macro import bank_of_canada as boc

# Trimmed from https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/
# (2026-10-10): the empty h2#schedule anchor, the tables in sibling blocks after it, linked
# and plain cells. The table *before* the anchor must be ignored.
SCHEDULE_HTML = """
<table class="bocss-table"><caption>Schedule for 1999</caption>
<tr><td>January 5</td><td>Interest rate announcement</td></tr></table>
<div class="cfct-module cfct-widget-module-bochtml">
    <div class="cfct-mod-content"><h2 id="schedule"></h2>
</div>
</div></div></div><div class="row cfct_row_2col"><div class="col-sm-6 cfct-block">
<div class="cfct-mod-content"><table class="bocss-table table-refresh-2024">
<caption  class="table-caption">
Schedule for 2026
</caption>
<colgroup>
	<col width="25%">
	<col width="75%">
</colgroup>
<thead>
<tr><th scope="col">Dates</th><th scope="col">Publications</th></tr>
</thead>
<tbody>
<tr>
<td>January 28</td>
<td><a href="https://www.bankofcanada.ca/2026/01/fad-press-release-2026-01-28/">Interest rate announcement</a> and <em><a href="https://www.bankofcanada.ca/publications/mpr/mpr-2026-01-28/">Monetary Policy Report</a></em></td>
</tr>
<tr>
<td>March 18</td>
<td><a href="https://www.bankofcanada.ca/2026/03/fad-press-release-2026-03-18/">Interest rate announcement</a></td>
</tr>
<tr>
<td>October 28</td>
<td>Interest rate announcement and <em>Monetary Policy Report</em></td>
</tr>
<tr>
<td>December 9</td>
<td>Interest rate announcement</td>
</tr>
<tr>
<td>November 4</td>
<td>Financial System Review</td>
</tr>
</tbody>
</table>
</div>
</div><div class="col-sm-6 cfct-block">
<div class="cfct-mod-content"><table class="bocss-table table-refresh-2024">
<caption class="table-caption">
Schedule for 2027
</caption>
<tbody>
<tr>
<td>January 27</td>
<td>Interest rate announcement and <em>Monetary Policy Report</em></td>
</tr>
</tbody>
</table>
</div></div>
"""

VALET = {
    "observations": [
        {"d": "2026-09-30", "V39079": {"v": "2.50"}},
        {"d": "2026-10-01", "V39079": {"v": "2.25"}},
        {"d": "2026-10-02", "V39079": {"v": ""}},
        {"d": "bad", "V39079": {"v": "2.25"}},
        {"d": "2026-10-03"},
    ]
}


class FakeBank:
    def __init__(self, schedule: str = SCHEDULE_HTML, valet=VALET, status: int = 200) -> None:
        self.schedule = schedule
        self.valet = valet
        self.status = status
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params") or {}))
        request = httpx.Request("GET", url)
        if "valet" in url:
            return httpx.Response(self.status, json=self.valet, request=request)
        return httpx.Response(self.status, text=self.schedule, request=request)


def _freeze_today(monkeypatch, day: date) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(day.year, day.month, day.day, 12, tzinfo=tz or UTC)

    monkeypatch.setattr(boc, "datetime", FrozenDatetime)


def test_parse_schedule_reads_years_and_mpr_flags():
    announcements = boc.parse_boc_schedule(SCHEDULE_HTML)

    assert [(a.day, a.mpr) for a in announcements] == [
        (date(2026, 1, 28), True),
        (date(2026, 3, 18), False),
        (date(2026, 10, 28), True),
        (date(2026, 12, 9), False),
        (date(2027, 1, 27), True),
    ]


def test_announcement_event_carries_the_mpr_flag():
    event = boc.announcement_event(
        boc.BocAnnouncement(day=date(2026, 10, 28), mpr=True, label="Interest rate announcement")
    )
    assert (event.source, event.external_id, event.kind, event.country) == (
        "bankofcanada",
        "boc:2026-10-28",
        "boc_rate_announcement",
        "CA",
    )
    assert event.raw == {"mpr": True, "label": "Interest rate announcement"}


def test_parse_schedule_ignores_everything_before_the_anchor():
    assert boc.parse_boc_schedule(SCHEDULE_HTML.replace('id="schedule"', 'id="other"')) == []


@pytest.mark.parametrize(
    "cells",
    [
        ["January 28"],
        ["Smarch 3", "Interest rate announcement"],
        ["February 30", "Interest rate announcement"],
        ["TBD", "Interest rate announcement"],
    ],
)
def test_announcement_from_cells_rejects_unreadable_rows(cells):
    assert boc.announcement_from_cells(2026, cells) is None


def test_parse_valet_skips_blank_and_malformed_values():
    assert boc.parse_valet_observations(VALET, "V39079") == [
        (date(2026, 9, 30), 2.5),
        (date(2026, 10, 1), 2.25),
    ]
    with pytest.raises(boc.BankOfCanadaProviderError, match="shape"):
        boc.parse_valet_observations({"nope": []}, "V39079")


def test_rate_fetch_is_full_history_first_then_incremental_with_overlap(monkeypatch):
    bank = FakeBank()
    monkeypatch.setattr(httpx, "get", bank.get)
    session_cm = make_session_scope()
    connector = boc.BankOfCanadaRateConnector(session_factory=session_cm)

    assert connector.fetch() == 2
    assert bank.calls[-1] == (boc.VALET_URL.format(series="V39079"), {})
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        for row in session.scalars(select(MacroObservation)):
            row.first_seen_at = row.fetched_at = marker

    bank.valet = {"observations": [{"d": "2026-10-01", "V39079": {"v": "2.00"}}]}
    assert connector.fetch() == 1
    assert bank.calls[-1][1] == {"start_date": "2026-09-01"}  # newest minus 30 days

    with session_cm() as session:
        rows = {r.date: r for r in session.scalars(select(MacroObservation))}
    assert rows[date(2026, 10, 1)].value == 2.0  # revised in place
    assert rows[date(2026, 10, 1)].source == "bankofcanada"
    assert rows[date(2026, 10, 1)].series_id == "V39079"
    assert rows[date(2026, 10, 1)].first_seen_at.replace(tzinfo=UTC) == marker  # insert-only
    assert rows[date(2026, 10, 1)].fetched_at.replace(tzinfo=UTC) > marker
    assert rows[date(2026, 9, 30)].fetched_at.replace(tzinfo=UTC) == marker  # not re-read


def test_rate_fetch_honours_an_explicit_since(monkeypatch):
    bank = FakeBank()
    monkeypatch.setattr(httpx, "get", bank.get)
    boc.BankOfCanadaRateConnector(session_factory=make_session_scope()).fetch(
        since=date(2020, 1, 1)
    )
    assert bank.calls[-1][1] == {"start_date": "2020-01-01"}


@pytest.mark.parametrize(
    ("bank", "match"),
    [
        (FakeBank(status=500), "HTTP 500"),
        (FakeBank(valet=["not", "a", "dict"]), "shape"),
    ],
)
def test_rate_fetch_failures_raise_provider_errors(monkeypatch, bank, match):
    monkeypatch.setattr(httpx, "get", bank.get)
    with pytest.raises(boc.BankOfCanadaProviderError, match=match):
        boc.BankOfCanadaRateConnector(session_factory=make_session_scope()).fetch()


def test_rate_fetch_wraps_transport_and_json_errors(monkeypatch):
    def broken(url, **kwargs):
        raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", broken)
    with pytest.raises(boc.BankOfCanadaProviderError, match="ReadTimeout"):
        boc.BankOfCanadaRateConnector(session_factory=make_session_scope()).fetch()

    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, text="<html>", request=httpx.Request("GET", url)),
    )
    with pytest.raises(boc.BankOfCanadaProviderError, match="non-JSON"):
        boc.BankOfCanadaRateConnector(session_factory=make_session_scope()).fetch()


def test_schedule_fetch_upserts_and_deletes_a_moved_future_date(monkeypatch):
    _freeze_today(monkeypatch, date(2026, 10, 10))
    monkeypatch.setattr(httpx, "get", FakeBank().get)
    session_cm = make_session_scope()
    connector = boc.BankOfCanadaScheduleConnector(session_factory=session_cm)

    assert connector.fetch() == 5
    with session_cm() as session:
        october = session.scalar(
            select(EconomicEvent).where(EconomicEvent.external_id == "boc:2026-10-28")
        )
        assert (october.kind, october.country, october.source) == (
            "boc_rate_announcement",
            "CA",
            "bankofcanada",
        )
        assert october.raw["mpr"] is True

    # December moves to the 10th; March (past) is no longer listed.
    moved = SCHEDULE_HTML.replace("December 9", "December 10").replace("March 18", "")
    monkeypatch.setattr(httpx, "get", FakeBank(schedule=moved).get)
    connector.fetch()

    with session_cm() as session:
        ids = set(session.scalars(select(EconomicEvent.external_id)))
    assert "boc:2026-12-09" not in ids
    assert {"boc:2026-12-10", "boc:2026-03-18"} <= ids


def test_schedule_fetch_raises_on_zero_announcements(monkeypatch):
    monkeypatch.setattr(httpx, "get", FakeBank(schedule="<html></html>").get)
    with pytest.raises(boc.BankOfCanadaProviderError, match="markup"):
        boc.BankOfCanadaScheduleConnector(session_factory=make_session_scope()).fetch()
