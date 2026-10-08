"""SEC EDGAR registration-filing connector -> ``ipo_events`` (source ``sec_edgar``).

Reads EDGAR's **daily form index** rather than the "latest filings" Atom feed, because the
index is gap-proof: every business day has one file, so a run after downtime walks the days it
missed instead of losing whatever scrolled off a feed. Official description:
https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data

- ``/Archives/edgar/daily-index/<YYYY>/QTR<n>/form.<YYYYMMDD>.idx`` — one per business day,
  sorted by form type. Fixed-width text: a preamble, a header naming ``Form Type``,
  ``Company Name``, ``CIK``, ``Date Filed``, ``File Name``, a dashed rule, then one line per
  filing. Form types can contain spaces (``SC 13G``, ``RW WD``), so the form column is cut at
  the header's ``Company Name`` offset, and the last three fields are split from the right.
- ``index.json`` in each quarter directory lists the files that exist. EDGAR answers **403**,
  not 404, for a day with no index (weekend, holiday, not yet published) — the same status it
  gives an undeclared client — so days are taken from the listing, never guessed, and any
  non-200 on a listed file is a real failure.
- "Date Filed" is the filing's own date: a confidential ``DRS`` made public today carries the
  weeks-old submission date.

Fair access (same page): at most **10 requests/second**, and a declared ``User-Agent`` of the
form ``"Name admin@domain"`` is required. Requests are paced module-wide (``_PACER``) below
that ceiling, since EDGAR is many small requests.

What is stored: every ``S-1``/``F-1``/``DRS`` (``filed``), their ``/A`` amendments
(``amended``), ``424B4`` (``priced``) and ``RW`` (``withdrawn``). These forms are also used for
resales, SPACs and follow-ons — telling them apart is the consumer's job, so a new row's
``raw`` carries the filer's SIC code and any listed tickers, from
``data.sec.gov/submissions/CIK##########.json`` (one request per new filer per run).

A run is one transaction. With no explicit ``since``, it restarts ``OVERLAP_DAYS`` before the
newest ``fetched_at`` it wrote, so a failed run — which commits nothing — is retried in full.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data_lake.db.models import IpoEvent
from data_lake.ingestion.base import Connector
from data_lake.ingestion.market.ipo_common import upsert_ipo_event
from data_lake.settings import LakeSettings
from data_lake.runtime import SessionFactory

logger = logging.getLogger(__name__)

ARCHIVES_URL = "https://www.sec.gov/Archives/edgar"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

#: EDGAR form type -> ipo_events.stage. Anything else in the index is ignored.
STAGE_BY_FORM = {
    "S-1": "filed",
    "F-1": "filed",
    "DRS": "filed",
    "S-1/A": "amended",
    "F-1/A": "amended",
    "DRS/A": "amended",
    "424B4": "priced",
    "RW": "withdrawn",
}

#: 8 requests/second: under the SEC's 10/s ceiling with room for clock jitter.
MIN_REQUEST_INTERVAL_SECONDS = 0.125

#: First run with nothing stored: how far back to start.
DEFAULT_LOOKBACK_DAYS = 7
#: Later runs restart this many days before the newest fetch, re-reading days whose index
#: was not yet published last time. Re-reading is idempotent.
OVERLAP_DAYS = 3
#: A quarter directory this new may not exist yet; its 403 means "no files", not a failure.
NEW_QUARTER_GRACE_DAYS = 4


class SecEdgarProviderError(RuntimeError):
    """EDGAR refused a request or returned something unparseable."""


class Pacer:
    """Spaces calls at least ``min_interval`` seconds apart. Clock and sleep are injectable."""

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self.min_interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


#: Module-level on purpose: the SEC's limit is per client, so every connector instance in a
#: process shares one budget.
_PACER = Pacer(MIN_REQUEST_INTERVAL_SECONDS)


@dataclass(frozen=True)
class IndexEntry:
    """One line of a daily form index."""

    form_type: str
    company_name: str
    cik: str  # as the index prints it, unpadded
    date_filed: date
    file_name: str  # edgar/data/<cik>/<accession>.txt

    @property
    def accession(self) -> str:
        return self.file_name.rsplit("/", 1)[-1].removesuffix(".txt")

    @property
    def filing_index_url(self) -> str:
        folder = self.accession.replace("-", "")
        return f"{ARCHIVES_URL}/data/{int(self.cik)}/{folder}/{self.accession}-index.htm"


def parse_form_index(text: str) -> list[IndexEntry]:
    """Every filing line in one ``form.<YYYYMMDD>.idx``. Raises when the layout is not one."""
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.startswith("Form Type")), None)
    rule = next((i for i, line in enumerate(lines) if set(line.strip()) == {"-"}), None)
    if header is None or rule is None or rule < header:
        raise SecEdgarProviderError("EDGAR form index has no recognisable header")
    company_col = lines[header].index("Company Name")
    entries = []
    for line in lines[rule + 1 :]:
        parts = line.rsplit(None, 3)
        if len(parts) != 4 or not parts[1].isdigit() or not re.fullmatch(r"\d{8}", parts[2]):
            continue
        head, cik, filed, file_name = parts
        entries.append(
            IndexEntry(
                form_type=head[:company_col].strip(),
                company_name=head[company_col:].strip(),
                cik=cik,
                date_filed=datetime.strptime(filed, "%Y%m%d").date(),
                file_name=file_name,
            )
        )
    return entries


def quarters_between(since: date, until: date) -> list[tuple[int, int]]:
    """(year, quarter) for every calendar quarter the window touches, oldest first."""
    quarters = []
    year, quarter = since.year, (since.month - 1) // 3 + 1
    while (year, quarter) <= (until.year, (until.month - 1) // 3 + 1):
        quarters.append((year, quarter))
        year, quarter = (year + 1, 1) if quarter == 4 else (year, quarter + 1)
    return quarters


def listed_days(listing: Any) -> list[date]:
    """The days a quarter's ``index.json`` has a form index for."""
    try:
        items = listing["directory"]["item"]
    except (KeyError, TypeError):
        raise SecEdgarProviderError("unexpected EDGAR directory listing shape") from None
    days = []
    for item in items:
        match = re.fullmatch(r"form\.(\d{8})\.idx", str(item.get("name", "")))
        if match:
            days.append(datetime.strptime(match.group(1), "%Y%m%d").date())
    return sorted(days)


def _as_date(value: date | str | None) -> date | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, date) else date.fromisoformat(value)


class EdgarRegistrationConnector(Connector):
    name = "sec_edgar"

    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
        pacer: Pacer | None = None,
        enrich: bool = True,
    ) -> None:
        super().__init__(settings=settings, session_factory=session_factory)
        self._pacer = pacer or _PACER
        self._enrich = enrich

    def fetch(
        self, since: date | str | None = None, until: date | str | None = None, **kwargs
    ) -> int:
        """Upsert every tracked filing in the daily indexes from ``since`` to ``until``."""
        user_agent = self.settings.sec_user_agent
        if not user_agent.strip():
            raise RuntimeError("SEC_USER_AGENT is not set")
        last = _as_date(until) or datetime.now(UTC).date()
        first = _as_date(since)
        now = datetime.now(UTC)
        with self.session() as session:
            if first is None:
                first = self._default_since(session, last)
            entries = self._collect(first, last, user_agent)
            profiles: dict[str, dict[str, Any]] = {}
            for accession, (entry, co_registrants) in entries.items():
                fields = _entry_fields(entry, co_registrants)
                if self._enrich and not _exists(session, self.name, accession):
                    profile = self._profile(entry.cik, user_agent, profiles)
                    fields["symbol"] = (profile.get("tickers") or [None])[0]
                    fields["exchange"] = (profile.get("exchanges") or [None])[0]
                    fields["raw"].update(profile)
                upsert_ipo_event(session, self.name, accession, fields, now)
        return len(entries)

    def _default_since(self, session: Session, until: date) -> date:
        newest = session.scalar(
            select(func.max(IpoEvent.fetched_at)).where(IpoEvent.source == self.name)
        )
        if newest is None:
            return until - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        return newest.date() - timedelta(days=OVERLAP_DAYS)

    def _collect(
        self, since: date, until: date, user_agent: str
    ) -> dict[str, tuple[IndexEntry, list[str]]]:
        """Tracked filings by accession, each with any co-registrant CIKs sharing it."""
        found: dict[str, tuple[IndexEntry, list[str]]] = {}
        for year, quarter in quarters_between(since, until):
            for day in self._quarter_days(year, quarter, until, user_agent):
                if not since <= day <= until:
                    continue
                url = f"{ARCHIVES_URL}/daily-index/{year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
                for entry in parse_form_index(self._get(url, user_agent).text):
                    if entry.form_type not in STAGE_BY_FORM:
                        continue
                    if entry.accession in found:
                        found[entry.accession][1].append(entry.cik.zfill(10))
                    else:
                        found[entry.accession] = (entry, [])
        return found

    def _quarter_days(self, year: int, quarter: int, until: date, user_agent: str) -> list[date]:
        url = f"{ARCHIVES_URL}/daily-index/{year}/QTR{quarter}/index.json"
        quarter_start = date(year, 3 * quarter - 2, 1)
        if quarter_start > until - timedelta(days=NEW_QUARTER_GRACE_DAYS):
            response = self._send(url, user_agent)
            if response.status_code in (403, 404):
                return []  # the directory is created with its first file
            _raise_for_status(response)
        else:
            response = self._get(url, user_agent)
        return listed_days(response.json())

    def _profile(
        self, cik: str, user_agent: str, cache: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """SIC and tickers for one filer, or {} when EDGAR cannot say — never fatal."""
        if cik not in cache:
            url = SUBMISSIONS_URL.format(cik=cik.zfill(10))
            try:
                data = self._get(url, user_agent).json()
                cache[cik] = {
                    "sic": data.get("sic") or None,
                    "sic_description": data.get("sicDescription") or None,
                    "tickers": list(data.get("tickers") or []),
                    "exchanges": list(data.get("exchanges") or []),
                }
            except (SecEdgarProviderError, httpx.HTTPError, ValueError, AttributeError) as exc:
                logger.warning("sec_edgar: no filer profile for CIK %s (%s)", cik, exc)
                cache[cik] = {}
        return cache[cik]

    def _send(self, url: str, user_agent: str) -> httpx.Response:
        self._pacer.wait()
        return httpx.get(
            url,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=30,
        )

    def _get(self, url: str, user_agent: str) -> httpx.Response:
        response = self._send(url, user_agent)
        _raise_for_status(response)
        return response


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SecEdgarProviderError(
            f"EDGAR request for {exc.request.url.path} failed with HTTP "
            f"{exc.response.status_code}. A 403 also means an undeclared or over-rate client: "
            "check SEC_USER_AGENT and request pacing."
        ) from None


def _exists(session: Session, source: str, accession: str) -> bool:
    return (
        session.scalar(
            select(IpoEvent.id).where(IpoEvent.source == source, IpoEvent.external_id == accession)
        )
        is not None
    )


def _entry_fields(entry: IndexEntry, co_registrants: list[str]) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "form_type": entry.form_type,
        "company_name": entry.company_name,
        "cik": entry.cik,
        "date_filed": entry.date_filed.isoformat(),
        "file_name": entry.file_name,
    }
    if co_registrants:
        raw["co_registrant_ciks"] = co_registrants
    return {
        "company_name": entry.company_name,
        "cik": entry.cik.zfill(10),
        "stage": STAGE_BY_FORM[entry.form_type],
        "form_type": entry.form_type,
        "filed_at": datetime.combine(entry.date_filed, datetime.min.time(), tzinfo=UTC),
        "url": entry.filing_index_url,
        "raw": raw,
    }
