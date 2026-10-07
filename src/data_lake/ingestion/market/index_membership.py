"""Point-in-time S&P 500 membership from fja05680/sp500 (MIT licence, spans since 1996).

A backtest that starts from today's constituents only ever holds the survivors, which
inflates every stock strategy. This connector stores *who was in the index on each date* as
one row per membership span, so a consumer can build the universe as of any day.

Source: ``sp500_ticker_start_end.csv`` — columns ``ticker,start_date,end_date``, an empty end
meaning a current member. Tickers are as spelled during the span: a rename is a new span,
and class shares keep their dot (``BRK.B``). Nothing here guesses that two spans are the same
company; see :mod:`data_lake.ingestion.market.index_pricing`.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date

import httpx
from sqlalchemy import select

from data_lake.db.models import IndexMembership
from data_lake.ingestion.base import Connector

__all__ = [
    "INDEX_CODE",
    "MIN_CURRENT_MEMBERS",
    "SP500_START_END_URL",
    "MembershipSpan",
    "Sp500MembershipConnector",
    "parse_start_end_csv",
]

SP500_START_END_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)
INDEX_CODE = "SP500"

#: The index has ~500 members. A file parsing to fewer current ones is a truncated or wrong
#: download, and applying it would delete every span it is missing — so refuse it instead.
MIN_CURRENT_MEMBERS = 400


@dataclass(frozen=True)
class MembershipSpan:
    symbol: str
    start: date
    end: date | None  # None = current member


def parse_start_end_csv(text: str) -> list[MembershipSpan]:
    """Parse ``ticker,start_date,end_date`` rows. Raises ``ValueError`` on a malformed file."""
    reader = csv.DictReader(io.StringIO(text))
    required = {"ticker", "start_date", "end_date"}
    if reader.fieldnames is None or not required <= {f.strip() for f in reader.fieldnames}:
        raise ValueError(f"expected columns {sorted(required)}, got {reader.fieldnames}")

    spans: list[MembershipSpan] = []
    seen: set[tuple[str, date]] = set()
    for line, row in enumerate(reader, start=2):
        row = {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        symbol = row["ticker"].upper()
        if not symbol:
            continue
        try:
            start = date.fromisoformat(row["start_date"])
            end = date.fromisoformat(row["end_date"]) if row["end_date"] else None
        except ValueError as exc:
            raise ValueError(f"line {line}: bad date in {row!r}") from exc
        if (symbol, start) in seen:
            raise ValueError(f"line {line}: duplicate span {symbol} from {start}")
        seen.add((symbol, start))
        spans.append(MembershipSpan(symbol=symbol, start=start, end=end))
    return spans


def _http_get_text(url: str) -> str:
    """One GET. Isolated so tests can stub it without network."""
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return response.text


class Sp500MembershipConnector(Connector):
    name = "fja05680"

    def fetch(self, **kwargs) -> int:
        """Mirror the reference file into ``index_memberships``; returns the spans upserted.

        Upserts on (index, symbol, start, source), moves ``end_date`` when a member is
        removed, and deletes this index+source's rows the file no longer lists. Pricing
        columns on a surviving row are kept.
        """
        spans = parse_start_end_csv(_http_get_text(SP500_START_END_URL))
        current = sum(1 for span in spans if span.end is None)
        if current < MIN_CURRENT_MEMBERS:
            raise RuntimeError(
                f"{SP500_START_END_URL} parsed to {current} current members "
                f"(< {MIN_CURRENT_MEMBERS}); refusing to apply what looks like a bad download"
            )

        with self.session() as session:
            existing = {
                (row.symbol, row.start_date): row
                for row in session.scalars(
                    select(IndexMembership).where(
                        IndexMembership.index_code == INDEX_CODE,
                        IndexMembership.source == self.name,
                    )
                )
            }
            for span in spans:
                row = existing.pop((span.symbol, span.start), None)
                if row is None:
                    session.add(
                        IndexMembership(
                            index_code=INDEX_CODE,
                            symbol=span.symbol,
                            start_date=span.start,
                            end_date=span.end,
                            source=self.name,
                        )
                    )
                elif row.end_date != span.end:
                    row.end_date = span.end
            for stale in existing.values():
                session.delete(stale)
        return len(spans)
