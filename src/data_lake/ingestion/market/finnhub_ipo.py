"""Finnhub IPO-calendar connector -> ``ipo_events`` (source ``finnhub``).

Docs: https://finnhub.io/docs/api/ipo-calendar. ``GET /calendar/ipo?from=&to=`` returns
``{"ipoCalendar": [...]}``, each row carrying exactly ``date``, ``exchange``, ``name``,
``numberOfShares``, ``price``, ``status``, ``symbol``, ``totalSharesValue`` (checked against a
live response, 2026-10-08). What that response showed, and what the parsing below is for:

- ``status`` is ``filed`` | ``expected`` | ``priced`` | ``withdrawn``. ``date`` means the
  filing date for ``filed`` and the (expected) pricing date otherwise.
- ``price`` is a string, ``"14.00-16.00"`` or ``"10.00"``, and null until a range is set.
- ``exchange`` is null until a deal is scheduled, and ``symbol`` is null *or* ``""`` until one
  is reserved — so neither can be part of the key. See ``models.IpoEvent``.
- ``totalSharesValue`` is ``0`` when unknown, which is stored as None, not as a $0 deal.
- One company can appear twice in a response (a withdrawn registration and its refiling, same
  day). Rows sharing a key collapse to the latest date, then the more advanced stage.

**History and the row cap.** The free plan serves the calendar back to at least 2010, but one
response holds at most ``MAX_ROWS_PER_CALL`` rows — the *latest* ones, with nothing saying the
rest were dropped (checked 2026-10-10: 2020-07-01..2020-12-31 returned 200 rows, the oldest
dated 2020-10-09). So a window whose response fills the cap is split in half and both halves
re-asked, down to a single day; a backfill is just ``fetch`` over a long window. Calls are
paced under the free plan's 60/minute.

Finnhub reports a SPAC under its **unit** ticker (``CONXU``, ``NBA'U``), and some SPAC rows
carry the name of the company the SPAC later merged with (``SoFi Technologies, Inc.`` under
``IPOEU``). Rows are stored as Finnhub sends them; ``ipo_pricing`` decides which are listings.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from data_lake.ingestion.base import Connector, SessionFactory, stable_hash
from data_lake.ingestion.market.ipo_common import upsert_ipo_event
from data_lake.ingestion.market.sec_edgar import Pacer
from data_lake.ingestion.news.finnhub_news import FinnhubProviderError
from data_lake.settings import LakeSettings

__all__ = ["FinnhubIpoCalendarConnector", "FinnhubProviderError"]

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"

#: Default window: a week back catches late status changes, 90 days forward the pipeline.
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_LOOKAHEAD_DAYS = 90

#: The most rows one /calendar/ipo response returns; a full response may have dropped some.
MAX_ROWS_PER_CALL = 200

#: 1.1 s apart: under the free plan's 60 calls/minute with room for clock jitter.
MIN_REQUEST_INTERVAL_SECONDS = 1.1

#: Module-level on purpose: Finnhub's limit is per key, so every instance shares one budget.
_PACER = Pacer(MIN_REQUEST_INTERVAL_SECONDS)

#: Finnhub status -> ipo_events.stage. An unlisted status is skipped with a warning rather
#: than guessed into the vocabulary.
STAGE_BY_STATUS = {
    "filed": "filed",
    "expected": "expected",
    "priced": "priced",
    "withdrawn": "withdrawn",
}

#: Tie-break when one key appears twice on the same date: the further-along row wins.
_STAGE_RANK = {"withdrawn": 0, "filed": 1, "expected": 2, "priced": 3}

#: Trailing legal-form words dropped before hashing, so "Acme Holdings Ltd." and
#: "ACME HOLDINGS LIMITED" are one deal across the stages Finnhub spells them differently in.
_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc"}
    | {"llc", "lp"}
)

_RAW_FIELDS = (
    "date",
    "exchange",
    "name",
    "numberOfShares",
    "price",
    "status",
    "symbol",
    "totalSharesValue",
)


def normalized_name(name: str) -> str:
    """Casefolded, punctuation-free, without trailing legal-form words."""
    words = re.sub(r"[^0-9a-z]+", " ", name.casefold()).split()
    while len(words) > 1 and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def deal_key(name: str) -> str:
    """The calendar row's external id: stable across re-dating, symbol and exchange changes."""
    return stable_hash(f"finnhub-ipo:{normalized_name(name)}")


def parse_price_range(price: object) -> tuple[float | None, float | None]:
    """``"18.00-20.00"`` -> (18.0, 20.0); ``"20.00"`` -> (20.0, 20.0); anything else -> Nones."""
    if not isinstance(price, str | int | float) or isinstance(price, bool):
        return None, None
    numbers = re.findall(r"\d+(?:\.\d+)?", str(price).replace(",", ""))
    if len(numbers) == 1:
        return float(numbers[0]), float(numbers[0])
    if len(numbers) == 2:
        low, high = sorted(float(n) for n in numbers)
        return low, high
    return None, None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return int(value)
    return None


def _trimmed_raw(item: dict[str, Any]) -> dict[str, Any]:
    """The documented field set only — whatever Finnhub adds later is not stored blind."""
    return {key: item.get(key) for key in _RAW_FIELDS}


def row_fields(item: dict[str, Any]) -> dict[str, Any] | None:
    """One calendar row as ``ipo_events`` columns, or None when it cannot be stored."""
    name = item.get("name")
    stage = STAGE_BY_STATUS.get(str(item.get("status") or "").strip().lower())
    if not isinstance(name, str) or not normalized_name(name) or stage is None:
        return None
    try:
        row_date = date.fromisoformat(str(item.get("date")))
    except ValueError:
        row_date = None
    price_low, price_high = parse_price_range(item.get("price"))
    value = _positive_int(item.get("totalSharesValue"))
    return {
        "company_name": name.strip(),
        "symbol": (item.get("symbol") or "").strip().upper() or None,
        "exchange": (item.get("exchange") or "").strip()[:32] or None,
        "stage": stage,
        # A filing date for "filed", the pricing date otherwise; "withdrawn" keeps what the
        # row already had, since its date is the withdrawal's.
        "filed_at": (
            datetime.combine(row_date, datetime.min.time(), tzinfo=UTC)
            if row_date and stage == "filed"
            else None
        ),
        "expected_date": row_date if stage in ("expected", "priced") else None,
        "price_low": price_low,
        "price_high": price_high,
        "shares": _positive_int(item.get("numberOfShares")),
        "deal_value_usd": float(value) if value is not None else None,
        "raw": _trimmed_raw(item),
    }


def _collapse(items: list[Any]) -> dict[str, dict[str, Any]]:
    """Storable rows by key; a duplicated key keeps the latest date, then the later stage."""
    chosen: dict[str, tuple[tuple[str, int], dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        fields = row_fields(item)
        if fields is None:
            logger.warning(
                "finnhub ipo row skipped: status %r / name %r not storable",
                item.get("status"),
                item.get("name"),
            )
            continue
        rank = (str(item.get("date") or ""), _STAGE_RANK[fields["stage"]])
        key = deal_key(fields["company_name"])
        if key not in chosen or rank >= chosen[key][0]:
            chosen[key] = (rank, fields)
    return {key: fields for key, (_, fields) in chosen.items()}


def _default_window() -> tuple[str, str]:
    today = datetime.now(UTC).date()
    return (
        (today - timedelta(days=DEFAULT_LOOKBACK_DAYS)).isoformat(),
        (today + timedelta(days=DEFAULT_LOOKAHEAD_DAYS)).isoformat(),
    )


class FinnhubIpoCalendarConnector(Connector):
    name = "finnhub"

    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
        pacer: Pacer | None = None,
    ) -> None:
        super().__init__(settings=settings, session_factory=session_factory)
        self._pacer = pacer or _PACER

    def fetch(self, date_from: str = "", date_to: str = "", **kwargs) -> int:
        """Upsert every calendar row dated within the window. Returns rows upserted.

        Any window length works: one that fills a response is split until none does, so a
        historical backfill is ``fetch(date_from="2013-01-01", date_to=...)``. The whole
        window is one transaction, written only once every request has succeeded.
        """
        settings = self.settings
        if not settings.finnhub_key:
            raise RuntimeError("FINNHUB_KEY is not set (see .env.example)")
        default_from, default_to = _default_window()
        # Validate the format loudly rather than letting Finnhub silently return [].
        window_from = date.fromisoformat(date_from or default_from)
        window_to = date.fromisoformat(date_to or default_to)

        rows = _collapse(self._calendar(window_from, window_to, settings.finnhub_key))
        now = datetime.now(UTC)
        with self.session() as session:
            for key, fields in rows.items():
                upsert_ipo_event(session, self.name, key, fields, now)
        return len(rows)

    def _calendar(self, date_from: date, date_to: date, api_key: str) -> list[Any]:
        """Every row dated ``date_from..date_to``, halving any window that fills a response."""
        items = self._request(date_from, date_to, api_key)
        if len(items) < MAX_ROWS_PER_CALL:
            return items
        if date_from >= date_to:
            logger.warning(
                "finnhub ipo calendar: %s alone fills a response; rows may be missing", date_from
            )
            return items
        middle = date_from + (date_to - date_from) // 2
        return self._calendar(date_from, middle, api_key) + self._calendar(
            middle + timedelta(days=1), date_to, api_key
        )

    def _request(self, date_from: date, date_to: date, api_key: str) -> list[Any]:
        self._pacer.wait()
        response = _get_ipo_calendar(date_from.isoformat(), date_to.isoformat(), api_key)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise FinnhubProviderError(
                f"Finnhub IPO calendar request failed with HTTP {status}. Check FINNHUB_KEY, "
                "plan access, and rate limits. API key omitted from error."
            ) from None

        payload = response.json()
        items = payload.get("ipoCalendar") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise FinnhubProviderError("unexpected Finnhub IPO-calendar response shape")
        return items


def _get_ipo_calendar(date_from: str, date_to: str, api_key: str) -> httpx.Response:
    return httpx.get(
        f"{BASE_URL}/calendar/ipo",
        params={"from": date_from, "to": date_to},
        headers={"X-Finnhub-Token": api_key},
        timeout=30,
    )
