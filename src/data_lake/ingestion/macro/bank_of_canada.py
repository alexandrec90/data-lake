"""Bank of Canada connectors: the policy rate -> ``macro_observations``, the announcement
schedule -> ``economic_events``. Source ``bankofcanada``; neither needs a key.

- **Policy rate** — Valet, the Bank's open API:
  ``GET https://www.bankofcanada.ca/valet/observations/V39079/json?start_date=YYYY-MM-DD``
  returns ``{observations: [{d: "2026-10-08", V39079: {v: "2.25"}}]}``, the value a string.
  Incremental like FRED: ``OVERLAP_DAYS`` before the newest stored date, else full history.
- **Announcement schedule** — no feed, so it is read from
  https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/ (markup checked
  2026-10-10): from ``h2#schedule`` onward, one ``table.bocss-table`` per year, its ``caption``
  reading "Schedule for 2026", each row ``<td>January 28</td><td>Interest rate announcement
  and <em>Monetary Policy Report</em></td>``. Rows naming a Monetary Policy Report get
  ``raw["mpr"] = True``.

As with the FOMC page, a schedule parse that finds nothing raises, and a future announcement
missing from a good parse is deleted.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from data_lake.ingestion.base import Connector
from data_lake.ingestion.macro.common import (
    OVERLAP_DAYS,
    ScheduledEvent,
    TagStackParser,
    delete_vanished_future_events,
    fetch_page,
    has_class,
    newest_observation_date,
    squash,
    upsert_events,
    upsert_observations,
)

__all__ = [
    "BankOfCanadaProviderError",
    "BankOfCanadaRateConnector",
    "BankOfCanadaScheduleConnector",
    "BocAnnouncement",
    "parse_boc_schedule",
    "parse_valet_observations",
]

SOURCE = "bankofcanada"
POLICY_RATE_SERIES = "V39079"
VALET_URL = "https://www.bankofcanada.ca/valet/observations/{series}/json"
SCHEDULE_URL = "https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/"
KIND = "boc_rate_announcement"

_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_MONTH_DAY = re.compile(r"\b([A-Za-z]{3,})\.?\s+(\d{1,2})\b")
_YEAR = re.compile(r"\b(\d{4})\b")


class BankOfCanadaProviderError(RuntimeError):
    """The Bank's API or schedule page failed, or returned nothing readable."""


def parse_valet_observations(payload: Any, series: str) -> list[tuple[date, float]]:
    """``(date, value)`` for every observation of ``series`` carrying a numeric value."""
    rows = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise BankOfCanadaProviderError("unexpected Valet response shape")
    points = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(series), dict):
            continue
        try:
            day = date.fromisoformat(str(row.get("d")))
            value = float(str(row[series].get("v")))
        except ValueError:
            continue
        if value == value:  # NaN never equals itself
            points.append((day, value))
    return points


class BankOfCanadaRateConnector(Connector):
    name = "bankofcanada"

    def fetch(self, since: date | None = None, **kwargs) -> int:
        """Upsert the policy rate (Valet ``V39079``); returns observations written."""
        if since is None:
            with self.session() as session:
                newest = newest_observation_date(session, SOURCE, POLICY_RATE_SERIES)
            since = newest - timedelta(days=OVERLAP_DAYS) if newest else None
        params = {"start_date": since.isoformat()} if since else {}
        payload = _get_valet(POLICY_RATE_SERIES, params)
        points = parse_valet_observations(payload, POLICY_RATE_SERIES)
        with self.session() as session:
            return upsert_observations(
                session, SOURCE, POLICY_RATE_SERIES, points, datetime.now(UTC)
            )


def _get_valet(series: str, params: dict[str, str]) -> Any:
    try:
        response = httpx.get(VALET_URL.format(series=series), params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise BankOfCanadaProviderError(
            f"Valet request for {series} failed with HTTP {exc.response.status_code}"
        ) from None
    except httpx.HTTPError as exc:
        raise BankOfCanadaProviderError(
            f"Valet request for {series} failed ({type(exc).__name__})"
        ) from None
    except ValueError:
        raise BankOfCanadaProviderError(f"Valet returned a non-JSON body for {series}") from None


@dataclass(frozen=True)
class BocAnnouncement:
    """One scheduled rate announcement."""

    day: date
    mpr: bool  # published with a Monetary Policy Report
    label: str  # the row's own description


def announcement_from_cells(year: int, cells: Sequence[str]) -> BocAnnouncement | None:
    """One announcement from a schedule row's cells, or None when the row is not one."""
    if len(cells) < 2 or "rate announcement" not in cells[1].lower():
        return None
    match = _MONTH_DAY.search(cells[0])
    month = _MONTHS.get(match.group(1)[:3].lower()) if match else None
    if match is None or month is None:
        return None
    try:
        day = date(year, month, int(match.group(2)))
    except ValueError:
        return None
    return BocAnnouncement(
        day=day, mpr="monetary policy report" in cells[1].lower(), label=cells[1]
    )


class _ScheduleParser(TagStackParser):
    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.year: int | None = None
        self.announcements: list[BocAnnouncement] = []
        self._caption: list[str] = []
        self._cells: list[str] = []
        self._cell: list[str] = []

    def on_start(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "h2" and attrs.get("id") == "schedule":
            self.active = True
        if not self.active:
            return
        if tag == "table" and has_class(attrs, "bocss-table"):
            self.year = None
        elif tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._cells = []
        elif tag == "td":
            self._cell = []

    def handle_data(self, data: str) -> None:
        if not self.active or not self.inside("table", "bocss-table"):
            return
        if self.inside("caption"):
            self._caption.append(data)
        elif self.inside("td"):
            self._cell.append(data)

    def on_end(self, tag: str, attrs: dict[str, str]) -> None:
        if not self.active:
            return
        if tag == "caption" and self.inside("table", "bocss-table"):
            match = _YEAR.search(squash("".join(self._caption)))
            self.year = int(match.group(1)) if match else None
        elif tag == "td" and self.inside("table", "bocss-table"):
            self._cells.append(squash("".join(self._cell)))
        elif tag == "tr" and self.inside("table", "bocss-table") and self.year is not None:
            announcement = announcement_from_cells(self.year, self._cells)
            if announcement is not None:
                self.announcements.append(announcement)


def parse_boc_schedule(html: str) -> list[BocAnnouncement]:
    """Every rate announcement in the page's schedule tables, oldest first."""
    parser = _ScheduleParser()
    parser.feed(html)
    parser.close()
    unique = {announcement.day: announcement for announcement in parser.announcements}
    return [unique[day] for day in sorted(unique)]


def announcement_event(announcement: BocAnnouncement) -> ScheduledEvent:
    return ScheduledEvent(
        source=SOURCE,
        external_id=f"boc:{announcement.day.isoformat()}",
        kind=KIND,
        name="Bank of Canada interest rate announcement",
        country="CA",
        event_date=announcement.day,
        raw={"mpr": announcement.mpr, "label": announcement.label},
    )


class BankOfCanadaScheduleConnector(Connector):
    name = "bankofcanada"

    def fetch(self, **kwargs) -> int:
        """Upsert every scheduled rate announcement; returns announcements written."""
        html = fetch_page(SCHEDULE_URL, BankOfCanadaProviderError, "Bank of Canada schedule")
        announcements = parse_boc_schedule(html)
        if not announcements:
            raise BankOfCanadaProviderError(
                "no rate announcement could be read from the schedule page; its markup has "
                "probably changed (see ingestion/macro/bank_of_canada.py)"
            )
        events = [announcement_event(announcement) for announcement in announcements]
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
