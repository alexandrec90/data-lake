"""SEC XBRL company-concept connector -> ``sec_facts`` (source ``sec_xbrl``).

Docs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces.

- ``https://www.sec.gov/files/company_tickers.json`` maps tickers to CIKs:
  ``{"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}``, the CIK an
  int, zero-padded here to 10 digits. Read once per call. Only SEC registrants are in it, so
  a TSX listing has no CIK and is skipped (and counted).
- ``https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/<Concept>.json``
  returns ``{units: {USD: [{start, end, val, accn, fy, fp, form, filed, frame?}], ...}}``.
  **404** means the company never reported that concept — a skip, not an error.

Every fact is stored, including the same period restated in later filings (see
``models.SecFact``): ``fy``/``fp`` can be null, ``frame`` sits on one fact per period only, and
10-Q cash-flow values are year-to-date, all kept as filed.

The default concepts describe buybacks: cash spent repurchasing common stock, the authorised
programme size, and shares repurchased.

Fair access: 10 requests/second with a declared ``User-Agent``. This shares ``sec_edgar``'s
module-level pacer, so the two connectors together stay under the one per-client limit.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select

from data_lake.db.models import Instrument, SecFact
from data_lake.ingestion.base import Connector, run_each
from data_lake.ingestion.market.sec_edgar import _PACER, Pacer
from data_lake.runtime import SessionFactory
from data_lake.settings import LakeSettings

__all__ = ["DEFAULT_CONCEPTS", "SecFactsConnector", "SecXbrlProviderError", "parse_concept_facts"]

logger = logging.getLogger(__name__)

SOURCE = "sec_xbrl"
TAXONOMY = "us-gaap"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{concept}.json"

DEFAULT_CONCEPTS = (
    "PaymentsForRepurchaseOfCommonStock",
    "StockRepurchaseProgramAuthorizedAmount1",
    "StockRepurchasedDuringPeriodShares",
)


class SecXbrlProviderError(RuntimeError):
    """SEC refused a request or returned something unparseable."""


def parse_ticker_map(payload: Any) -> dict[str, str]:
    """Upper-cased ticker -> 10-digit CIK."""
    if not isinstance(payload, dict):
        raise SecXbrlProviderError("unexpected company_tickers.json shape")
    tickers: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, dict):
            continue
        ticker, cik = entry.get("ticker"), entry.get("cik_str")
        if isinstance(ticker, str) and isinstance(cik, int | str) and str(cik).isdigit():
            tickers.setdefault(ticker.strip().upper(), str(cik).zfill(10))
    return tickers


def ticker_candidates(symbol: str) -> list[str]:
    """SEC spells share classes with a dash (``BRK-B``); instruments may use ``.`` or a space."""
    base = symbol.strip().upper()
    return list(dict.fromkeys([base, base.replace(".", "-"), base.replace(" ", "-")]))


def _date_or_none(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def parse_concept_facts(payload: Any, cik: str, concept: str) -> dict[str, dict[str, Any]]:
    """``sec_facts`` column values by ``external_id`` for every usable fact in a response."""
    units = payload.get("units") if isinstance(payload, dict) else None
    if not isinstance(units, dict):
        raise SecXbrlProviderError(f"unexpected companyconcept shape for {concept}")
    taxonomy = str(payload.get("taxonomy") or TAXONOMY)
    facts: dict[str, dict[str, Any]] = {}
    for unit, rows in units.items():
        for row in rows if isinstance(rows, list) else []:
            fact = fact_values(row, cik=cik, taxonomy=taxonomy, concept=concept, unit=str(unit))
            if fact is not None:
                facts[fact[0]] = fact[1]
    return facts


def fact_values(
    row: Any, *, cik: str, taxonomy: str, concept: str, unit: str
) -> tuple[str, dict[str, Any]] | None:
    """``(external_id, sec_facts column values)`` for one fact, or None when it is unusable
    (no end date, filing date or accession number, or a non-numeric value)."""
    if not isinstance(row, dict):
        return None
    end, filed = _date_or_none(row.get("end")), _date_or_none(row.get("filed"))
    value, accn = row.get("val"), row.get("accn")
    if end is None or filed is None or not accn:
        return None
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    start = _date_or_none(row.get("start"))
    external_id = (
        f"{accn}:{taxonomy}:{concept}:{unit}:{start.isoformat() if start else ''}:{end.isoformat()}"
    )
    fy, fp, frame = row.get("fy"), row.get("fp"), row.get("frame")
    return external_id, {
        "cik": cik,
        "taxonomy": taxonomy[:16],
        "concept": concept,
        "unit": unit[:16],
        "period_start": start,
        "period_end": end,
        "value": float(value),
        "fiscal_year": fy if isinstance(fy, int) and not isinstance(fy, bool) else None,
        "fiscal_period": str(fp)[:4] if fp else None,
        "form": str(row.get("form") or "")[:16],
        "filed": filed,
        "frame": str(frame)[:16] if frame else None,
    }


class SecFactsConnector(Connector):
    name = "sec_xbrl"

    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        super().__init__(settings=settings, session_factory=session_factory)
        self._pacer = pacer or _PACER

    def fetch(
        self, symbols: Sequence[str] = (), concepts: Sequence[str] = DEFAULT_CONCEPTS, **kwargs
    ) -> int:
        """Upsert every fact of each concept for each symbol's filer; returns facts written."""
        user_agent = self.settings.sec_user_agent
        if not user_agent.strip():
            raise RuntimeError("SEC_USER_AGENT is not set")
        wanted = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
        if not wanted:
            raise ValueError("symbols is required")
        if not concepts:
            raise ValueError("concepts is required")

        try:
            ticker_payload = self._get(TICKERS_URL, user_agent).json()
        except ValueError:
            raise SecXbrlProviderError("company_tickers.json is not JSON") from None
        ticker_map = parse_ticker_map(ticker_payload)
        work: list[tuple[str, str, str]] = []
        missing = []
        for symbol in wanted:
            cik = next((ticker_map[t] for t in ticker_candidates(symbol) if t in ticker_map), None)
            if cik is None:
                missing.append(symbol)
                continue
            work.extend((symbol, cik, concept) for concept in concepts)
        if missing:
            logger.info(
                "sec_xbrl: %d symbol(s) have no SEC CIK, skipped: %s", len(missing), missing
            )
        if not work:
            return 0
        return run_each(
            work,
            lambda item: self._fetch_concept(*item, user_agent=user_agent),
            what="SEC companyconcept request",
        )

    def _fetch_concept(self, symbol: str, cik: str, concept: str, *, user_agent: str) -> int:
        url = CONCEPT_URL.format(cik=cik, taxonomy=TAXONOMY, concept=concept)
        response = self._send(url, user_agent)
        if response.status_code == 404:
            return 0  # never reported: not an error
        _raise_for_status(response)
        try:
            payload = response.json()
        except ValueError:
            raise SecXbrlProviderError(f"non-JSON companyconcept body for {concept}") from None
        facts = parse_concept_facts(payload, cik, concept)
        if not facts:
            return 0
        now = datetime.now(UTC)
        with self.session() as session:
            instrument_id = session.scalar(
                select(Instrument.id)
                .where(Instrument.symbol == symbol, Instrument.currency == "USD")
                .order_by(Instrument.id)
                .limit(1)
            )
            existing = {
                row.external_id: row
                for row in session.scalars(
                    select(SecFact).where(
                        SecFact.source == SOURCE, SecFact.cik == cik, SecFact.concept == concept
                    )
                )
            }
            for external_id, values in facts.items():
                row = existing.get(external_id)
                if row is None:
                    session.add(
                        SecFact(
                            source=SOURCE,
                            external_id=external_id,
                            instrument_id=instrument_id,
                            first_seen_at=now,
                            fetched_at=now,
                            **values,
                        )
                    )
                else:
                    for column, value in values.items():
                        setattr(row, column, value)
                    if instrument_id is not None:
                        row.instrument_id = instrument_id
                    row.fetched_at = now
        return len(facts)

    def _send(self, url: str, user_agent: str) -> httpx.Response:
        self._pacer.wait()
        try:
            return httpx.get(
                url,
                headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise SecXbrlProviderError(
                f"SEC request for {httpx.URL(url).path} failed ({type(exc).__name__})"
            ) from None

    def _get(self, url: str, user_agent: str) -> httpx.Response:
        response = self._send(url, user_agent)
        _raise_for_status(response)
        return response


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code != 200:
        raise SecXbrlProviderError(
            f"SEC request for {response.request.url.path} failed with HTTP "
            f"{response.status_code}. A 403 also means an undeclared or over-rate client: "
            "check SEC_USER_AGENT and request pacing."
        )
