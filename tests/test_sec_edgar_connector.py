from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import IpoEvent
from data_lake.ingestion.market import sec_edgar as se

UA = "data-lake tests test@example.com"
ARCHIVE = se.ARCHIVES_URL
Q3 = f"{ARCHIVE}/daily-index/2026/QTR3"
Q4 = f"{ARCHIVE}/daily-index/2026/QTR4"

# The preamble and header of a real daily form index (form.20260729.idx), verbatim.
PREAMBLE = """\
Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Jul 29, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/




Form Type   Company Name                                                  CIK
      Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
"""

# Real lines from the same file (trailing padding stripped), one per shape that matters:
# a form type containing a space, an untracked 424B3, and each tracked form. The DRS made
# public on 07-29 carries its 07-15 submission date.
REAL_LINES = """\
APP ORDR         Blue Tractor ETF Trust                                        1668791     20260729    edgar/data/1668791/9999999997-26-001276.txt
424B3            AKZO NOBEL NV                                                 3124        20260729    edgar/data/3124/0001193125-26-322163.txt
424B4            Game Your Game Inc.                                           2111846     20260729    edgar/data/2111846/0001213900-26-082494.txt
DRS              Teamshares Inc                                                2048951     20260717    edgar/data/2048951/0001193125-26-307962.txt
F-1              BTC Digital Ltd.                                              1796514     20260729    edgar/data/1796514/0001213900-26-082801.txt
RW               Aspargo Labs, Inc.                                            1805092     20260729    edgar/data/1805092/0001999371-26-016275.txt
S-1              Teamshares Inc                                                2048951     20260729    edgar/data/2048951/0001193125-26-324170.txt
S-1/A            Attovia Therapeutics, Inc.                                    2058707     20260729    edgar/data/2058707/0001193125-26-323618.txt
"""

TRACKED_ACCESSIONS = {
    "0001213900-26-082494": ("424B4", "priced"),
    "0001193125-26-307962": ("DRS", "filed"),
    "0001213900-26-082801": ("F-1", "filed"),
    "0001999371-26-016275": ("RW", "withdrawn"),
    "0001193125-26-324170": ("S-1", "filed"),
    "0001193125-26-323618": ("S-1/A", "amended"),
}


def _line(form: str, company: str, cik: str, filed: str, accession: str) -> str:
    """A synthetic index line in the real column widths."""
    return f"{form:<17}{company:<62}{cik:<12}{filed:<12}edgar/data/{cik}/{accession}.txt"


def _listing(*names: str) -> dict:
    return {"directory": {"name": "daily-index/2026/QTR3/", "item": [{"name": n} for n in names]}}


def _submissions(sic: str, tickers: list[str], exchanges: list[str]) -> dict:
    return {"sic": sic, "sicDescription": f"SIC {sic}", "tickers": tickers, "exchanges": exchanges}


class FakeEdgar:
    """Routes httpx.get by URL; an unrouted URL answers 403, as EDGAR does."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, object]] = {}
        self.calls: list[str] = []

    def route(self, url: str, body: object, status: int = 200) -> None:
        self.routes[url] = (status, body)

    def get(self, url, headers, timeout):
        assert headers["User-Agent"] == UA
        assert timeout == 30
        self.calls.append(url)
        status, body = self.routes.get(url, (403, "Forbidden"))
        request = httpx.Request("GET", url)
        if isinstance(body, str):
            return httpx.Response(status, text=body, request=request)
        return httpx.Response(status, json=body, request=request)

    def submissions_calls(self) -> list[str]:
        return [url for url in self.calls if "submissions" in url]


class NoWait:
    def __init__(self) -> None:
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


@pytest.fixture
def edgar(monkeypatch):
    monkeypatch.setattr(SETTINGS, "sec_user_agent", UA)
    fake = FakeEdgar()
    monkeypatch.setattr(se.httpx, "get", fake.get)
    return fake


def _serve_july_29(edgar: FakeEdgar) -> None:
    days = ("form.20260728.idx", "form.20260729.idx", "form.20260731.idx")
    edgar.route(f"{Q3}/index.json", _listing("company.20260729.idx", *days))
    edgar.route(f"{Q3}/form.20260728.idx", PREAMBLE)
    edgar.route(f"{Q3}/form.20260729.idx", PREAMBLE + REAL_LINES)
    edgar.route(
        se.SUBMISSIONS_URL.format(cik="0002048951"), _submissions("7374", ["TSHR"], ["Nasdaq"])
    )
    for cik in ("0002111846", "0001796514", "0001805092", "0002058707"):
        edgar.route(se.SUBMISSIONS_URL.format(cik=cik), _submissions("2834", [], []))


def _connector(session_cm, **kwargs) -> se.EdgarRegistrationConnector:
    kwargs.setdefault("pacer", NoWait())
    return se.EdgarRegistrationConnector(session_factory=session_cm, **kwargs)


def _rows(session_cm) -> dict[str, IpoEvent]:
    with session_cm() as session:
        return {row.external_id: row for row in session.scalars(select(IpoEvent)).all()}


def test_parse_form_index_reads_the_real_layout():
    entries = se.parse_form_index(PREAMBLE + REAL_LINES)

    assert len(entries) == 8
    first = entries[0]
    assert (first.form_type, first.company_name, first.cik) == (
        "APP ORDR",
        "Blue Tractor ETF Trust",
        "1668791",
    )
    drs = entries[3]
    assert (drs.form_type, drs.date_filed) == ("DRS", date(2026, 7, 17))
    assert drs.accession == "0001193125-26-307962"
    assert drs.filing_index_url == (
        "https://www.sec.gov/Archives/edgar/data/2048951/000119312526307962/"
        "0001193125-26-307962-index.htm"
    )
    assert entries[-1].form_type == "S-1/A"


def test_index_entry_derives_accession_and_filing_index_url():
    entry = se.IndexEntry(
        form_type="S-1",
        company_name="Beneficient",
        cik="1775734",
        date_filed=date(2026, 7, 29),
        file_name="edgar/data/1775734/0001493152-26-035225.txt",
    )
    assert entry.accession == "0001493152-26-035225"
    assert entry.filing_index_url == (
        "https://www.sec.gov/Archives/edgar/data/1775734/000149315226035225/"
        "0001493152-26-035225-index.htm"
    )


def test_parse_form_index_skips_lines_that_are_not_filings():
    text = PREAMBLE + "garbage\n\n" + _line("S-1", "Acme", "123", "2026072X", "a-b") + "\n"
    assert se.parse_form_index(text) == []


@pytest.mark.parametrize("text", ["", "Forbidden", "Form Type  Company Name\nno rule here\n"])
def test_parse_form_index_rejects_a_non_index(text):
    with pytest.raises(se.SecEdgarProviderError, match="header"):
        se.parse_form_index(text)


def test_quarters_between_crosses_a_year():
    assert se.quarters_between(date(2025, 11, 30), date(2026, 4, 1)) == [
        (2025, 4),
        (2026, 1),
        (2026, 2),
    ]
    assert se.quarters_between(date(2026, 7, 1), date(2026, 7, 1)) == [(2026, 3)]


def test_listed_days_keeps_form_indexes_only():
    listing = _listing("form.20260702.idx", "master.20260701.idx", "form.20260701.idx", "x")
    assert se.listed_days(listing) == [date(2026, 7, 1), date(2026, 7, 2)]
    with pytest.raises(se.SecEdgarProviderError, match="listing"):
        se.listed_days({"unexpected": True})


def test_fetch_stores_tracked_forms_with_stage_and_filer_profile(edgar):
    session_cm = make_session_scope()
    _serve_july_29(edgar)

    count = _connector(session_cm).fetch(since="2026-07-28", until="2026-07-30")

    assert count == 6
    rows = _rows(session_cm)
    assert {acc: (r.form_type, r.stage) for acc, r in rows.items()} == TRACKED_ACCESSIONS
    s1 = rows["0001193125-26-324170"]
    assert (s1.source, s1.company_name, s1.cik) == ("sec_edgar", "Teamshares Inc", "0002048951")
    assert (s1.symbol, s1.exchange) == ("TSHR", "Nasdaq")
    assert s1.filed_at.replace(tzinfo=UTC) == datetime(2026, 7, 29, tzinfo=UTC)
    assert s1.url.endswith("/000119312526324170/0001193125-26-324170-index.htm")
    assert s1.raw["sic"] == "7374"
    assert s1.raw["file_name"] == "edgar/data/2048951/0001193125-26-324170.txt"
    assert rows["0001193125-26-307962"].filed_at.replace(tzinfo=UTC) == datetime(
        2026, 7, 17, tzinfo=UTC
    )
    rw = rows["0001999371-26-016275"]
    assert (rw.symbol, rw.raw["sic"], rw.raw["tickers"]) == (None, "2834", [])
    # The day outside the window is listed but never requested; one profile per filer.
    assert f"{Q3}/form.20260731.idx" not in edgar.calls
    assert len(edgar.submissions_calls()) == 5


def test_rerun_is_idempotent_and_keeps_first_seen_and_profile(edgar):
    session_cm = make_session_scope()
    _serve_july_29(edgar)
    _connector(session_cm).fetch(since="2026-07-28", until="2026-07-30")
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        for row in session.scalars(select(IpoEvent)):
            row.first_seen_at = marker
    edgar.calls.clear()

    assert _connector(session_cm).fetch(since="2026-07-28", until="2026-07-30") == 6

    rows = _rows(session_cm)
    assert len(rows) == 6
    assert edgar.submissions_calls() == []  # profiles are looked up on insert only
    s1 = rows["0001193125-26-324170"]
    assert s1.first_seen_at.replace(tzinfo=UTC) == marker
    assert s1.fetched_at.replace(tzinfo=UTC) > marker
    assert (s1.symbol, s1.raw["sic"]) == ("TSHR", "7374")  # not erased by the refresh


def test_co_registrants_share_one_row(edgar):
    session_cm = make_session_scope()
    lines = "\n".join(
        [
            _line("S-1", "Acme Parent Inc", "1000001", "20260729", "0000000001-26-000001"),
            _line("S-1", "Acme Guarantor LLC", "1000002", "20260729", "0000000001-26-000001"),
        ]
    )
    edgar.route(f"{Q3}/index.json", _listing("form.20260729.idx"))
    edgar.route(f"{Q3}/form.20260729.idx", PREAMBLE + lines + "\n")

    assert _connector(session_cm, enrich=False).fetch(since="2026-07-29", until="2026-07-29") == 1

    [row] = _rows(session_cm).values()
    assert (row.company_name, row.cik) == ("Acme Parent Inc", "0001000001")
    assert row.raw["co_registrant_ciks"] == ["0001000002"]
    assert edgar.submissions_calls() == []  # enrich=False


def test_failed_profile_lookup_is_not_fatal(edgar, caplog):
    session_cm = make_session_scope()
    _serve_july_29(edgar)
    del edgar.routes[se.SUBMISSIONS_URL.format(cik="0002048951")]  # now answers 403

    assert _connector(session_cm).fetch(since="2026-07-29", until="2026-07-29") == 6

    s1 = _rows(session_cm)["0001193125-26-324170"]
    assert s1.symbol is None
    assert "sic" not in s1.raw
    assert "0002048951" in caplog.text


def test_a_listed_day_that_fails_aborts_the_whole_run(edgar):
    """Nothing commits, so the next default run restarts before the gap."""
    session_cm = make_session_scope()
    _serve_july_29(edgar)
    edgar.route(f"{Q3}/form.20260728.idx", PREAMBLE + REAL_LINES)
    edgar.route(f"{Q3}/form.20260729.idx", "Forbidden", status=403)

    with pytest.raises(se.SecEdgarProviderError) as exc_info:
        _connector(session_cm).fetch(since="2026-07-28", until="2026-07-30")

    assert "HTTP 403" in str(exc_info.value)
    assert "SEC_USER_AGENT" in str(exc_info.value)
    assert UA not in str(exc_info.value)
    assert _rows(session_cm) == {}


def test_a_new_quarter_without_a_directory_is_empty_not_a_failure(edgar):
    session_cm = make_session_scope()
    _serve_july_29(edgar)  # Q4 is unrouted: 403, as EDGAR answers before its first file
    edgar.route(f"{Q3}/form.20260731.idx", PREAMBLE)

    assert _connector(session_cm).fetch(since="2026-07-29", until="2026-10-02") == 6
    assert f"{Q4}/index.json" in edgar.calls


def test_a_new_quarter_that_errors_otherwise_still_raises(edgar):
    _serve_july_29(edgar)
    edgar.route(f"{Q3}/form.20260731.idx", PREAMBLE)
    edgar.route(f"{Q4}/index.json", "Service Unavailable", status=503)

    with pytest.raises(se.SecEdgarProviderError, match="HTTP 503"):
        _connector(make_session_scope()).fetch(since="2026-07-29", until="2026-10-02")


def test_an_old_quarter_listing_failure_raises(edgar):
    with pytest.raises(se.SecEdgarProviderError, match="HTTP 403"):
        _connector(make_session_scope()).fetch(since="2026-07-29", until="2026-10-20")


def test_default_since_restarts_before_the_newest_fetch(edgar):
    session_cm = make_session_scope()
    connector = _connector(session_cm)
    with session_cm() as session:
        assert connector._default_since(session, date(2026, 7, 30)) == date(2026, 7, 23)
        session.add(
            IpoEvent(
                source="sec_edgar",
                external_id="x",
                company_name="x",
                stage="filed",
                first_seen_at=datetime(2026, 7, 1, tzinfo=UTC),
                fetched_at=datetime(2026, 7, 20, 23, 0, tzinfo=UTC),
            )
        )
        session.flush()
        assert connector._default_since(session, date(2026, 7, 30)) == date(2026, 7, 17)


def test_fetch_without_since_walks_from_the_default(edgar):
    session_cm = make_session_scope()
    _serve_july_29(edgar)

    assert _connector(session_cm).fetch(until=date(2026, 7, 30)) == 6  # default: 07-23 on


def test_every_request_is_paced(edgar):
    session_cm = make_session_scope()
    _serve_july_29(edgar)
    pacer = NoWait()

    _connector(session_cm, pacer=pacer).fetch(since="2026-07-28", until="2026-07-30")

    assert pacer.waits == len(edgar.calls) == 1 + 2 + 5  # listing, two days, five filers


@pytest.mark.parametrize("user_agent", ["", "   "])
def test_missing_user_agent_raises(user_agent):
    connector = se.EdgarRegistrationConnector(SimpleNamespace(sec_user_agent=user_agent))
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT is not set"):
        connector.fetch()


def test_configured_settings_without_user_agent_raise():
    """The suite's settings start with an empty user agent, like every credential."""
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        se.EdgarRegistrationConnector().fetch()


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_pacer_spaces_calls_to_the_interval():
    clock = FakeClock()
    pacer = se.Pacer(0.125, clock=clock, sleep=clock.sleep)

    pacer.wait()  # first call: no wait
    clock.now += 0.05
    pacer.wait()  # 0.05 elapsed: sleeps the remaining 0.075
    clock.now += 1.0
    pacer.wait()  # well past the interval: no wait

    assert clock.slept == [pytest.approx(0.075)]


def test_default_pacer_stays_under_the_sec_ceiling():
    assert 1 / se.MIN_REQUEST_INTERVAL_SECONDS < 10
    assert se._PACER.min_interval == se.MIN_REQUEST_INTERVAL_SECONDS
