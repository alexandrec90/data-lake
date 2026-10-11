"""What the macro connectors share: the two upserts, the future-removal rule, and the small
tag-stack HTML parser the two scraped calendars (FOMC, Bank of Canada) are built on.

The upserts follow the lake's conventions: ``first_seen_at`` is written on insert and never
again, ``fetched_at`` on every run, and a revised value overwrites in place.
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import EconomicEvent, MacroObservation

#: Incremental series runs restart this many days before the newest stored observation, so
#: a figure revised within the last month is re-read and overwritten.
OVERLAP_DAYS = 30

#: Sent to the two scraped pages. Neither documents a requirement; a named client is polite.
USER_AGENT = "Mozilla/5.0 (compatible; data-lake calendar fetch)"


@dataclass(frozen=True)
class ScheduledEvent:
    """One ``economic_events`` row, before it is written."""

    source: str
    external_id: str
    kind: str
    name: str
    country: str
    event_date: date
    start_date: date | None = None
    release_id: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def newest_observation_date(session: Session, source: str, series_id: str) -> date | None:
    return session.scalar(
        select(func.max(MacroObservation.date)).where(
            MacroObservation.source == source, MacroObservation.series_id == series_id
        )
    )


def upsert_observations(
    session: Session,
    source: str,
    series_id: str,
    points: Iterable[tuple[date, float]],
    now: datetime,
) -> int:
    """Insert or revise each ``(date, value)``; returns points written."""
    values: dict[date, float] = dict(points)  # a date listed twice keeps its last value
    if not values:
        return 0
    existing = {
        row.date: row
        for row in session.scalars(
            select(MacroObservation).where(
                MacroObservation.source == source,
                MacroObservation.series_id == series_id,
                MacroObservation.date >= min(values),
            )
        )
    }
    for day, value in values.items():
        row = existing.get(day)
        if row is None:
            session.add(
                MacroObservation(
                    source=source,
                    series_id=series_id,
                    date=day,
                    value=value,
                    first_seen_at=now,
                    fetched_at=now,
                )
            )
        else:
            row.value = value
            row.fetched_at = now
    return len(values)


def upsert_events(session: Session, events: Sequence[ScheduledEvent], now: datetime) -> int:
    """Insert or refresh each event by ``(source, external_id)``; returns events written."""
    by_key = {(event.source, event.external_id): event for event in events}
    for (source, external_id), event in by_key.items():
        row = session.scalar(
            select(EconomicEvent).where(
                EconomicEvent.source == source, EconomicEvent.external_id == external_id
            )
        )
        values = {
            "kind": event.kind,
            "name": event.name,
            "country": event.country,
            "event_date": event.event_date,
            "start_date": event.start_date,
            "release_id": event.release_id,
            "raw": event.raw,
        }
        if row is None:
            session.add(
                EconomicEvent(
                    source=source,
                    external_id=external_id,
                    first_seen_at=now,
                    fetched_at=now,
                    **values,
                )
            )
        else:
            for column, value in values.items():
                setattr(row, column, value)
            row.fetched_at = now
    return len(by_key)


def delete_vanished_future_events(
    session: Session,
    *,
    source: str,
    kind: str,
    keep: Collection[str],
    today: date,
    release_id: int | None = None,
) -> int:
    """Delete ``source``/``kind`` events dated after ``today`` whose ``external_id`` is not in
    ``keep``; with ``release_id``, only that release's. Returns rows deleted.

    A rescheduled event changes its key (the date is part of it), so without this the old
    date stays behind as an event that never happens. Callers invoke it only after a fetch
    that found at least one future event — never on an empty or failed one.
    """
    query = select(EconomicEvent).where(
        EconomicEvent.source == source,
        EconomicEvent.kind == kind,
        EconomicEvent.event_date > today,
    )
    if release_id is not None:
        query = query.where(EconomicEvent.release_id == release_id)
    wanted = set(keep)
    deleted = 0
    for row in session.scalars(query).all():
        if row.external_id not in wanted:
            session.delete(row)
            deleted += 1
    return deleted


def fetch_page(url: str, error: type[Exception], what: str) -> str:
    """GET one public HTML page, raising ``error`` (naming ``what``) on any failure."""
    try:
        response = httpx.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise error(f"{what} page returned HTTP {exc.response.status_code}") from None
    except httpx.HTTPError as exc:
        raise error(f"{what} page could not be fetched ({type(exc).__name__})") from None
    return response.text


#: Elements with no closing tag; never pushed on the stack.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
    | {"track", "wbr"}
)


class TagStackParser(HTMLParser):
    """An ``HTMLParser`` that tracks the open elements, tolerating unclosed ones.

    Subclasses ask :meth:`inside` whether text or a tag sits within an element carrying a
    given tag, class or id — enough to read a page by its CSS hooks without a dependency.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag not in _VOID:
            self.stack.append((tag, attributes))
        self.on_start(tag, attributes)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.on_start(tag, {name: value or "" for name, value in attrs})

    def handle_endtag(self, tag: str) -> None:
        if not any(open_tag == tag for open_tag, _ in self.stack):
            return  # a stray closing tag closes nothing
        while self.stack:
            open_tag, attributes = self.stack.pop()
            self.on_end(open_tag, attributes)
            if open_tag == tag:
                return

    def on_start(self, tag: str, attrs: dict[str, str]) -> None:
        """Called for every start tag, after it is pushed."""

    def on_end(self, tag: str, attrs: dict[str, str]) -> None:
        """Called for every element as it is closed, after it is popped."""

    def inside(self, tag: str | None = None, cls: str | None = None) -> bool:
        """Whether any open element matches ``tag`` and carries class ``cls``."""
        return any(
            (tag is None or open_tag == tag) and (cls is None or has_class(attrs, cls))
            for open_tag, attrs in self.stack
        )


def has_class(attrs: dict[str, str], cls: str) -> bool:
    return cls in attrs.get("class", "").split()


def squash(text: str) -> str:
    """Collapse runs of whitespace (including non-breaking spaces) to single spaces."""
    return " ".join(text.split())  # str.split() treats a non-breaking space as whitespace
