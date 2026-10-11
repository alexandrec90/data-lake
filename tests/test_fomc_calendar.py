from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from data_lake.db.models import EconomicEvent
from data_lake.ingestion.macro import common
from data_lake.ingestion.macro import fomc

# Trimmed from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (2026-10-10):
# the real classes, the real stray quote in `class="row fomc-meeting" "`, the unclosed <br>s,
# and one row of each shape — plain, SEP, notation vote, and a meeting across two months.
FOMC_HTML = """
<p><a href="#42828">2026</a> | <a href="#42827">2025</a><br />
Future Year: <a href="#45694">2027</a> <!--Next year: --></p>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="45694">2027 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting" ">
            <div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>June</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">8-9*</div>
            <div class="col-xs-12 col-md-4 col-lg-2"></div>
        </div>
        <div class="fomc-meeting--shaded row fomc-meeting" ">
            <div class="fomc-meeting--shaded fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>December</strong></div>
            <div class="fomc-meeting--shaded fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">7-8*</div>
        </div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42828">2026 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting" ">
            <div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>January</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">27-28</div>
            <div class="col-xs-12 col-md-4 col-lg-2">
               <strong>Statement:</strong><br>
               <a href="/monetarypolicy/files/monetary20260128a1.pdf">PDF</a> | <a href="/newsevents/pressreleases/monetary20260128a.htm">HTML</a><br>
            </div>
            <div class="col-xs-12 col-md-4 col-lg-4 fomc-meeting__minutes">
                <strong>Minutes:</strong><br>
                <br> (Released February 18, 2026)
            </div>
        </div>
        <div class="fomc-meeting--shaded row fomc-meeting" ">
            <div class="fomc-meeting--shaded fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>March</strong></div>
            <div class="fomc-meeting--shaded fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">17-18*</div>
        </div>
        <div class="row fomc-meeting" ">
            <div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>October</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">27-28</div>
        </div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="42827">2025 FOMC Meetings</a></h4></div>
<div class="fomc-meeting--shaded row fomc-meeting">
    <div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>August</strong></div>
    <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-2">22 (notation vote)</div>
    <div class="col-xs-12 col-md-4 col-lg-2"></div>
</div>
</div>
<div class="panel panel-default"><div class="panel-heading"><h4><a id="39116">2024 FOMC Meetings</a></h4></div>
        <div class="row fomc-meeting" ">
            <div class="fomc-meeting__month col-xs-5 col-sm-3 col-md-2"><strong>Apr/May</strong></div>
            <div class="fomc-meeting__date col-xs-4 col-sm-9 col-md-10 col-lg-1">30-1</div>
        </div>
</div>
"""


def test_parse_reads_every_row_shape():
    meetings = fomc.parse_fomc_calendar(FOMC_HTML)

    assert [(m.start_date, m.decision_date, m.sep, m.notation_vote) for m in meetings] == [
        (date(2024, 4, 30), date(2024, 5, 1), False, False),
        (date(2025, 8, 22), date(2025, 8, 22), False, True),
        (date(2026, 1, 27), date(2026, 1, 28), False, False),
        (date(2026, 3, 17), date(2026, 3, 18), True, False),
        (date(2026, 10, 27), date(2026, 10, 28), False, False),
        (date(2027, 6, 8), date(2027, 6, 9), True, False),
        (date(2027, 12, 7), date(2027, 12, 8), True, False),
    ]
    assert meetings[0].label == "Apr/May 30-1"


@pytest.mark.parametrize(
    ("month", "days", "expected"),
    [
        ("Jan/Feb", "31-1", (date(2023, 1, 31), date(2023, 2, 1))),
        ("Dec/Jan", "31-1", (date(2023, 12, 31), date(2024, 1, 1))),
        ("October", "31-1", (date(2023, 10, 31), date(2023, 11, 1))),  # one name, two months
        ("March", "15 (unscheduled)", (date(2023, 3, 15), date(2023, 3, 15))),
    ],
)
def test_meeting_from_cells_handles_month_boundaries(month, days, expected):
    meeting = fomc.meeting_from_cells(2023, month, days)
    assert meeting is not None
    assert (meeting.start_date, meeting.decision_date) == expected


@pytest.mark.parametrize(
    ("month", "days"),
    [("Smarch", "1-2"), ("March", "TBD"), ("February", "30-31"), ("March", "1-20"), ("", "1")],
)
def test_meeting_from_cells_rejects_what_is_not_a_meeting(month, days):
    assert fomc.meeting_from_cells(2023, month, days) is None


def test_meeting_event_keys_on_the_decision_day():
    meeting = fomc.FomcMeeting(
        start_date=date(2026, 3, 17),
        decision_date=date(2026, 3, 18),
        sep=True,
        notation_vote=False,
        label="March 17-18*",
    )
    event = fomc.meeting_event(meeting)
    assert (event.external_id, event.start_date, event.event_date) == (
        "fomc:2026-03-18",
        date(2026, 3, 17),
        date(2026, 3, 18),
    )
    assert event.raw == {"sep": True, "notation_vote": False, "label": "March 17-18*"}


def test_unreadable_rows_are_logged_and_skipped(caplog):
    html = FOMC_HTML.replace("<strong>October</strong>", "<strong>Smarch</strong>")
    meetings = fomc.parse_fomc_calendar(html)
    assert date(2026, 10, 28) not in {m.decision_date for m in meetings}
    assert "Smarch" in caplog.text


def test_rows_outside_any_year_panel_are_ignored():
    html = '<div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>May</strong></div><div class="fomc-meeting__date">1-2</div></div>'
    assert fomc.parse_fomc_calendar(html) == []


class FakePage:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status = status
        self.calls: list[str] = []

    def get(self, url, headers, timeout, follow_redirects):
        self.calls.append(url)
        request = httpx.Request("GET", url)
        return httpx.Response(self.status, text=self.text, request=request)


def _freeze_today(monkeypatch, day: date) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(day.year, day.month, day.day, 12, tzinfo=tz or UTC)

    monkeypatch.setattr(fomc, "datetime", FrozenDatetime)


def _events(session_cm) -> dict[str, EconomicEvent]:
    with session_cm() as session:
        return {row.external_id: row for row in session.scalars(select(EconomicEvent))}


def test_fetch_upserts_meetings_with_flags(monkeypatch):
    page = FakePage(FOMC_HTML)
    monkeypatch.setattr(common.httpx, "get", page.get)
    _freeze_today(monkeypatch, date(2026, 10, 10))
    session_cm = make_session_scope()

    assert fomc.FomcCalendarConnector(session_factory=session_cm).fetch() == 7

    events = _events(session_cm)
    march = events["fomc:2026-03-18"]
    assert (march.source, march.kind, march.country) == ("federalreserve", "fomc_meeting", "US")
    assert (march.start_date, march.event_date) == (date(2026, 3, 17), date(2026, 3, 18))
    assert march.raw["sep"] is True and march.raw["notation_vote"] is False
    assert events["fomc:2025-08-22"].raw["notation_vote"] is True
    assert page.calls == [fomc.CALENDAR_URL]


def test_a_moved_future_meeting_is_deleted_and_the_past_kept(monkeypatch):
    monkeypatch.setattr(common.httpx, "get", FakePage(FOMC_HTML).get)
    _freeze_today(monkeypatch, date(2026, 10, 10))
    session_cm = make_session_scope()
    fomc.FomcCalendarConnector(session_factory=session_cm).fetch()
    with session_cm() as session:
        first_seen = session.scalar(
            select(EconomicEvent.first_seen_at).where(
                EconomicEvent.external_id == "fomc:2027-06-09"
            )
        )

    # October moves a day later; March (past) drops off the page entirely.
    moved = FOMC_HTML.replace(
        ">27-28</div>\n        </div>\n</div>", ">28-29</div>\n        </div>\n</div>"
    )
    moved = moved.replace(">17-18*<", ">bogus<")
    monkeypatch.setattr(common.httpx, "get", FakePage(moved).get)
    fomc.FomcCalendarConnector(session_factory=session_cm).fetch()

    events = _events(session_cm)
    assert "fomc:2026-10-28" not in events
    assert "fomc:2026-10-29" in events
    assert "fomc:2026-03-18" in events  # past meetings are history, never deleted
    assert events["fomc:2027-06-09"].first_seen_at == first_seen


def test_a_page_with_no_meetings_raises_and_deletes_nothing(monkeypatch):
    monkeypatch.setattr(common.httpx, "get", FakePage(FOMC_HTML).get)
    _freeze_today(monkeypatch, date(2026, 10, 10))
    session_cm = make_session_scope()
    fomc.FomcCalendarConnector(session_factory=session_cm).fetch()

    monkeypatch.setattr(common.httpx, "get", FakePage("<html><body>redesigned</body></html>").get)
    with pytest.raises(fomc.FomcProviderError, match="markup"):
        fomc.FomcCalendarConnector(session_factory=session_cm).fetch()
    assert len(_events(session_cm)) == 7


def test_an_http_failure_raises_a_provider_error(monkeypatch):
    monkeypatch.setattr(common.httpx, "get", FakePage("", status=503).get)
    with pytest.raises(fomc.FomcProviderError, match="HTTP 503"):
        fomc.FomcCalendarConnector(session_factory=make_session_scope()).fetch()


def test_a_transport_failure_raises_a_provider_error(monkeypatch):
    def broken(url, **kwargs):
        raise httpx.ConnectError("boom", request=httpx.Request("GET", url))

    monkeypatch.setattr(common.httpx, "get", broken)
    with pytest.raises(fomc.FomcProviderError, match="ConnectError"):
        fomc.FomcCalendarConnector(session_factory=make_session_scope()).fetch()
