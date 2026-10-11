"""FOMC meeting calendar -> ``economic_events`` (source ``federalreserve``, kind
``fomc_meeting``).

The Fed publishes no machine-readable calendar, so this reads
https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (markup checked 2026-10-10;
the page covers 2021-2027):

- one ``div.panel.panel-default`` per year, headed by an ``h4`` reading "2026 FOMC Meetings".
  The year is taken from that text — the anchor ids vary.
- one ``div.fomc-meeting`` per meeting, holding ``div.fomc-meeting__month > strong`` ("March",
  or "Apr/May" for a meeting spanning two months) and ``div.fomc-meeting__date`` ("17-18*",
  "30-1", "22 (notation vote)"). A trailing ``*`` marks a Summary of Economic Projections.

``event_date`` is the last day (the decision), ``start_date`` the first. Unscheduled meetings
are not marked on the page and are stored like any other.

A parse that finds no meeting at all raises rather than reporting zero: the page changed, and
an empty result must not read as "the Fed cancelled every meeting". A future meeting missing
from a good parse is deleted (it was moved).
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from data_lake.ingestion.base import Connector
from data_lake.ingestion.macro.common import (
    ScheduledEvent,
    TagStackParser,
    delete_vanished_future_events,
    fetch_page,
    has_class,
    squash,
    upsert_events,
)

__all__ = ["FomcCalendarConnector", "FomcMeeting", "FomcProviderError", "parse_fomc_calendar"]

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
SOURCE = "federalreserve"
KIND = "fomc_meeting"

_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_YEAR_HEADING = re.compile(r"\b(\d{4})\s+FOMC\s+Meetings\b", re.IGNORECASE)
_DAYS = re.compile(r"^\s*(\d{1,2})(?:\s*[-\N{EN DASH}]\s*(\d{1,2}))?")


class FomcProviderError(RuntimeError):
    """The calendar page could not be fetched, or no meeting could be read from it."""


@dataclass(frozen=True)
class FomcMeeting:
    start_date: date
    decision_date: date
    sep: bool
    notation_vote: bool
    label: str  # the page's own month and date text, e.g. "Mar 17-18*"


def _month_number(text: str) -> int | None:
    return _MONTHS.get(text.strip()[:3].lower())


def meeting_from_cells(year: int, month_text: str, date_text: str) -> FomcMeeting | None:
    """One meeting from its month and date cells, or None when they do not read as one."""
    numbers = [_month_number(part) for part in month_text.split("/") if part.strip()]
    months = [number for number in numbers if number is not None]
    days = _DAYS.match(date_text)
    if not months or len(months) != len(numbers) or days is None:
        return None
    start_month = end_month = months[0]
    if len(months) > 1:
        end_month = months[-1]
    first_day = int(days.group(1))
    last_day = int(days.group(2) or first_day)
    if len(months) == 1 and last_day < first_day:
        end_month = start_month % 12 + 1  # "30-1" under a single month name
    end_year = year + 1 if end_month < start_month else year
    try:
        start = date(year, start_month, first_day)
        decision = date(end_year, end_month, last_day)
    except ValueError:
        return None
    if not timedelta(0) <= decision - start <= timedelta(days=7):
        return None
    return FomcMeeting(
        start_date=start,
        decision_date=decision,
        sep="*" in date_text,
        notation_vote="notation vote" in date_text.lower(),
        label=f"{month_text} {date_text}",
    )


class _FomcParser(TagStackParser):
    def __init__(self) -> None:
        super().__init__()
        self.year: int | None = None
        self.meetings: list[FomcMeeting] = []
        self.skipped: list[str] = []
        self._heading: list[str] = []
        self._month: list[str] = []
        self._date: list[str] = []

    def on_start(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "h4" and self.inside("div", "panel"):
            self._heading = []
        elif tag == "div" and has_class(attrs, "fomc-meeting"):
            self._month, self._date = [], []

    def handle_data(self, data: str) -> None:
        if self.inside("h4") and self.inside("div", "panel"):
            self._heading.append(data)
        if self.inside("div", "fomc-meeting__month") and self.inside("strong"):
            self._month.append(data)
        if self.inside("div", "fomc-meeting__date"):
            self._date.append(data)

    def on_end(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "h4" and self.inside("div", "panel"):
            match = _YEAR_HEADING.search(squash("".join(self._heading)))
            if match:
                self.year = int(match.group(1))
        elif tag == "div" and has_class(attrs, "fomc-meeting") and self.year is not None:
            month_text, date_text = squash("".join(self._month)), squash("".join(self._date))
            meeting = meeting_from_cells(self.year, month_text, date_text)
            if meeting is None:
                self.skipped.append(f"{self.year} {month_text!r} {date_text!r}")
            else:
                self.meetings.append(meeting)


def parse_fomc_calendar(html: str) -> list[FomcMeeting]:
    """Every meeting on the page, oldest first. Rows that do not parse are logged and skipped."""
    parser = _FomcParser()
    parser.feed(html)
    parser.close()
    for row in parser.skipped:
        logger.warning("fomc: unreadable meeting row %s", row)
    unique = {meeting.decision_date: meeting for meeting in parser.meetings}
    return [unique[day] for day in sorted(unique)]


def meeting_event(meeting: FomcMeeting) -> ScheduledEvent:
    return ScheduledEvent(
        source=SOURCE,
        external_id=f"fomc:{meeting.decision_date.isoformat()}",
        kind=KIND,
        name="FOMC meeting",
        country="US",
        event_date=meeting.decision_date,
        start_date=meeting.start_date,
        raw={"sep": meeting.sep, "notation_vote": meeting.notation_vote, "label": meeting.label},
    )


class FomcCalendarConnector(Connector):
    name = "federalreserve"

    def fetch(self, **kwargs) -> int:
        """Upsert every meeting on the Fed's calendar page; returns meetings written."""
        meetings = parse_fomc_calendar(fetch_page(CALENDAR_URL, FomcProviderError, "FOMC"))
        if not meetings:
            raise FomcProviderError(
                "no FOMC meeting could be read from the calendar page; its markup has "
                "probably changed (see ingestion/macro/fomc.py)"
            )
        events = [meeting_event(meeting) for meeting in meetings]
        today = datetime.now(UTC).date()
        with self.session() as session:
            written = upsert_events(session, events, datetime.now(UTC))
            if any(event.event_date > today for event in events):
                delete_vanished_future_events(
                    session,
                    source=SOURCE,
                    kind=KIND,
                    keep={event.external_id for event in events},
                    today=today,
                )
        return written
