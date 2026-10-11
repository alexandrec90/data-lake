"""FRED (Federal Reserve Bank of St. Louis) connectors: series observations ->
``macro_observations``, release calendar -> ``economic_events``. Source ``fred``.

Docs: https://fred.stlouisfed.org/docs/api/fred/. Every request carries ``file_type=json`` and
the key as the ``api_key`` **query parameter** — FRED has no header alternative, so the key is
in every request URL. Hence two rules here: an error never quotes a URL or an underlying
``httpx`` exception (both raise ``from None``), and :class:`RedactApiKey` is installed on the
``httpx`` logger, which logs each request URL at INFO.

- ``fred/series/observations`` -> ``{observations: [{date, value}]}``. ``value`` is a string,
  and the literal ``"."`` marks a missing observation, which is skipped.
- ``fred/release/dates`` with ``include_release_dates_with_no_data=true`` adds the **future**
  scheduled dates, which is what makes it a calendar. ``fred/release`` gives the name.
- Rate limit 120 requests/minute (HTTP 429 beyond); requests are paced module-wide at
  ``MIN_REQUEST_INTERVAL_SECONDS``.

Observations are the latest vintage, revised in place (see ``models.MacroObservation``).
Both connectors take many items per call; one failing is logged and the rest proceed, and a
call where every item failed raises (``ingestion.base.run_each``).
"""

import logging
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from data_lake.db.models import EconomicEvent
from data_lake.ingestion.base import Connector, run_each
from data_lake.ingestion.macro.common import (
    OVERLAP_DAYS,
    ScheduledEvent,
    delete_vanished_future_events,
    newest_observation_date,
    upsert_events,
    upsert_observations,
)
from data_lake.ingestion.market.sec_edgar import Pacer
from data_lake.runtime import SessionFactory
from data_lake.settings import LakeSettings

__all__ = [
    "FredProviderError",
    "FredReleaseCalendarConnector",
    "FredSeriesConnector",
    "RedactApiKey",
    "parse_observations",
    "parse_release_dates",
]

BASE_URL = "https://api.stlouisfed.org/fred"
SOURCE = "fred"

#: 100 requests/minute: under FRED's 120 with room for clock jitter.
MIN_REQUEST_INTERVAL_SECONDS = 0.6
#: Module-level on purpose: the limit is per key, so every connector instance shares it.
_PACER = Pacer(MIN_REQUEST_INTERVAL_SECONDS)

#: Page sizes: the documented maximum ``limit`` of each endpoint.
OBSERVATIONS_PAGE = 100_000
RELEASE_DATES_PAGE = 10_000

#: Release calendar: a release with nothing stored backfills from here; later runs re-read
#: the last ``CALENDAR_LOOKBACK_DAYS`` and everything scheduled.
CALENDAR_BACKFILL_SINCE = date(2000, 1, 1)
CALENDAR_LOOKBACK_DAYS = 30


class FredProviderError(RuntimeError):
    """FRED refused a request or returned something unparseable. Never carries the key."""


_KEY_IN_URL = re.compile(r"(api_key=)[^&\s\"'#]+")


class RedactApiKey(logging.Filter):
    """Rewrites ``api_key=<value>`` to ``api_key=REDACTED`` in a record's message."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "api_key=" in message:
            record.msg = _KEY_IN_URL.sub(r"\1REDACTED", message)
            record.args = ()
        return True


def install_key_redaction(logger_name: str = "httpx") -> None:
    """Attach :class:`RedactApiKey` to ``logger_name`` once (idempotent)."""
    target = logging.getLogger(logger_name)
    if not any(isinstance(existing, RedactApiKey) for existing in target.filters):
        target.addFilter(RedactApiKey())


# httpx logs "HTTP Request: GET <full url>" at INFO; with FRED that URL holds the key.
install_key_redaction()


def parse_observations(rows: Sequence[Any]) -> list[tuple[date, float]]:
    """``(date, value)`` for every usable observation; ``"."`` (missing) is skipped."""
    points = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            day = date.fromisoformat(str(row.get("date")))
            value = float(str(row.get("value")))
        except ValueError:
            continue  # "." is FRED's missing-value marker
        if value == value:  # NaN never equals itself
            points.append((day, value))
    return points


def parse_release_dates(rows: Sequence[Any]) -> list[date]:
    days = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            days.append(date.fromisoformat(str(row.get("date"))))
        except ValueError:
            continue
    return sorted(set(days))


class _FredClient:
    """GETs against the FRED API with the key attached, paced, and kept out of every error."""

    def __init__(self, api_key: str, pacer: Pacer) -> None:
        self._api_key = api_key
        self._pacer = pacer

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._pacer.wait()
        try:
            response = httpx.get(
                f"{BASE_URL}/{path}",
                params={**params, "api_key": self._api_key, "file_type": "json"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise FredProviderError(
                f"FRED {path} request failed ({type(exc).__name__}). API key omitted from error."
            ) from None
        if response.status_code != 200:
            raise FredProviderError(
                f"FRED {path} request failed with HTTP {response.status_code}"
                f"{self._detail(response)}. Check FRED_API_KEY and the rate limit "
                "(120/min). API key omitted from error."
            )
        try:
            payload = response.json()
        except ValueError:
            raise FredProviderError(f"FRED {path} returned a non-JSON body") from None
        if not isinstance(payload, dict):
            raise FredProviderError(f"unexpected FRED {path} response shape")
        return payload

    def paged(
        self, path: str, params: dict[str, Any], field: str, page_size: int
    ) -> list[dict[str, Any]]:
        """Every row of ``field`` across pages, following the response's ``count``."""
        rows: list[dict[str, Any]] = []
        while True:
            payload = self.get(path, {**params, "limit": page_size, "offset": len(rows)})
            page = payload.get(field)
            if not isinstance(page, list):
                raise FredProviderError(f"unexpected FRED {path} response shape")
            rows.extend(page)
            count = payload.get("count")
            if not page or not isinstance(count, int) or len(rows) >= count:
                return rows

    def _detail(self, response: httpx.Response) -> str:
        """FRED's own ``error_message``, scrubbed of the key in case it is ever echoed."""
        try:
            message = response.json().get("error_message")
        except (ValueError, AttributeError):
            return ""
        if not isinstance(message, str) or not message.strip():
            return ""
        message = _KEY_IN_URL.sub(r"\1REDACTED", message).replace(self._api_key, "REDACTED")
        return f" ({message.strip()[:200]})"


class _FredConnector(Connector):
    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        super().__init__(settings=settings, session_factory=session_factory)
        self._pacer = pacer or _PACER

    def _client(self) -> _FredClient:
        api_key = self.settings.fred_api_key
        if not api_key.strip():
            raise RuntimeError("FRED_API_KEY is not set (see .env.example)")
        return _FredClient(api_key.strip(), self._pacer)


class FredSeriesConnector(_FredConnector):
    name = "fred"

    def fetch(self, series_ids: Sequence[str] = (), since: date | None = None, **kwargs) -> int:
        """Upsert observations of each series; returns observations written.

        Incremental per series: from ``since`` when given, else ``OVERLAP_DAYS`` before the
        newest stored observation (re-reading recent revisions), else the full history.
        """
        client = self._client()
        ids = list(dict.fromkeys(s.strip().upper() for s in series_ids if s.strip()))
        if not ids:
            raise ValueError("series_ids is required")
        return run_each(
            ids, lambda series_id: self._fetch_series(client, series_id, since), what="FRED series"
        )

    def _fetch_series(self, client: _FredClient, series_id: str, since: date | None) -> int:
        if since is None:
            with self.session() as session:
                newest = newest_observation_date(session, SOURCE, series_id)
            since = newest - timedelta(days=OVERLAP_DAYS) if newest else None
        params: dict[str, Any] = {"series_id": series_id}
        if since is not None:
            params["observation_start"] = since.isoformat()
        rows = client.paged("series/observations", params, "observations", OBSERVATIONS_PAGE)
        points = parse_observations(rows)
        with self.session() as session:
            return upsert_observations(session, SOURCE, series_id, points, datetime.now(UTC))


class FredReleaseCalendarConnector(_FredConnector):
    name = "fred"

    def fetch(self, release_ids: Sequence[int] = (), since: date | None = None, **kwargs) -> int:
        """Upsert each release's dates from ``since`` onward, future scheduled dates included;
        returns events written.

        Without ``since``: a release with no stored dates backfills from
        ``CALENDAR_BACKFILL_SINCE``, any other re-reads the last ``CALENDAR_LOOKBACK_DAYS``.
        A future date the release no longer lists (it was rescheduled) is deleted.
        """
        client = self._client()
        ids = list(dict.fromkeys(int(release_id) for release_id in release_ids))
        if not ids:
            raise ValueError("release_ids is required")
        return run_each(
            ids,
            lambda release_id: self._fetch_release(client, release_id, since),
            what="FRED release",
        )

    def _fetch_release(self, client: _FredClient, release_id: int, since: date | None) -> int:
        today = datetime.now(UTC).date()
        if since is None:
            since = self._default_since(release_id, today)
        meta = client.get("release", {"release_id": release_id})
        name = _release_name(meta) or f"FRED release {release_id}"
        rows = client.paged(
            "release/dates",
            {
                "release_id": release_id,
                "realtime_start": since.isoformat(),
                "realtime_end": "9999-12-31",
                "include_release_dates_with_no_data": "true",
                "sort_order": "asc",
            },
            "release_dates",
            RELEASE_DATES_PAGE,
        )
        events = [
            ScheduledEvent(
                source=SOURCE,
                external_id=f"{release_id}:{day.isoformat()}",
                kind="data_release",
                name=name,
                country="US",
                event_date=day,
                release_id=release_id,
                raw={"release_id": release_id, "release_name": name, "date": day.isoformat()},
            )
            for day in parse_release_dates(rows)
            if day >= since
        ]
        with self.session() as session:
            written = upsert_events(session, events, datetime.now(UTC))
            if any(event.event_date > today for event in events):
                delete_vanished_future_events(
                    session,
                    source=SOURCE,
                    kind="data_release",
                    keep={event.external_id for event in events},
                    today=today,
                    release_id=release_id,
                )
        return written

    def _default_since(self, release_id: int, today: date) -> date:
        with self.session() as session:
            stored = session.scalar(
                select(EconomicEvent.id)
                .where(EconomicEvent.source == SOURCE, EconomicEvent.release_id == release_id)
                .limit(1)
            )
        if stored is None:
            return CALENDAR_BACKFILL_SINCE
        return today - timedelta(days=CALENDAR_LOOKBACK_DAYS)


def _release_name(meta: dict[str, Any]) -> str | None:
    releases = meta.get("releases")
    if isinstance(releases, list) and releases and isinstance(releases[0], dict):
        name = releases[0].get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None
