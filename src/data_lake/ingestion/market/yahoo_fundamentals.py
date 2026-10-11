"""Yahoo corporate-data connector (via yfinance) — dividends, splits, share counts,
statements, sector/industry, earnings report dates with EPS, and announced upcoming dividend
dates for one symbol.

Companion to yahoo.py (prices): both share the module-level throttle in yahoo_common, so a
single request-spacing budget covers all Yahoo traffic. yfinance is an unofficial scraper —
keep volume tiny.

Depth of what free yfinance actually serves (probed 2026-07):
- dividends: decades         - share counts: ~2015+       - sector/industry/name: current only
- statements: only ~4-5 annual / ~5-7 quarterly periods  - earnings dates: back to ~2001

Because statements are shallow, the design is **snapshot forward**: every run upserts the
latest statements and stamps ``first_seen`` (never updated) so future feature builds can
honestly answer "what did we know at time t?". ETFs return no statements/earnings/calendar —
those kinds are skipped gracefully (dividends and splits still ingested).

Upcoming events: ``get_earnings_dates`` lists the next report(s) with a null ``Reported EPS``,
and ``Ticker.calendar`` the announced ex-dividend and payment dates (``corporate_events``).
Both get re-dated, so an upcoming row the provider stops listing is deleted — but only when
the provider answered with something, since yfinance returns an empty frame or ``{}`` on
failure and that must not read as "every event was cancelled".
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from data_lake.db.models import (
    CorporateEvent,
    Dividend,
    FundamentalSnapshot,
    Instrument,
    ShareCount,
    StockSplit,
)
from data_lake.ingestion.base import Connector
from data_lake.ingestion.market.earnings_common import (
    delete_vanished_future_earnings,
    upsert_earnings_event,
)
from data_lake.ingestion.market.yahoo_common import (
    DOWNLOAD_TIMEOUT_SECONDS,
    YahooProviderError,
    get_or_create_instrument,
    run_within_timeout,
    throttle,
)

logger = logging.getLogger(__name__)

#: Value stored in every ``source`` column — the data provider, not the connector's routing key.
SOURCE = "yahoo"

#: ``get_earnings_dates`` column -> ``earnings_events`` column.
_EARNINGS_COLUMNS = {
    "EPS Estimate": "eps_estimate",
    "Reported EPS": "eps_actual",
    "Surprise(%)": "surprise_pct",
}

#: ``Ticker.calendar`` key -> ``corporate_events.kind``. Its other keys (earnings and revenue
#: estimates) are kept in ``raw`` only.
_CALENDAR_KINDS = {
    "Ex-Dividend Date": "ex_dividend",
    "Dividend Date": "dividend_payment",
}

#: A statement's figures only become public on the earnings report date. We infer that date as
#: the nearest earnings event *after* ``period_end`` and within this many days (statements are
#: filed weeks after quarter-end; ~120 days covers the slowest 10-K without matching next period).
MAX_REPORT_LAG_DAYS = 120

#: yfinance attribute per (freq, statement) — the six statement surfaces we snapshot.
_STATEMENT_ATTRS: dict[tuple[str, str], str] = {
    ("annual", "income"): "income_stmt",
    ("annual", "balance"): "balance_sheet",
    ("annual", "cashflow"): "cashflow",
    ("quarterly", "income"): "quarterly_income_stmt",
    ("quarterly", "balance"): "quarterly_balance_sheet",
    ("quarterly", "cashflow"): "quarterly_cashflow",
}


@dataclass
class RawFundamentals:
    """Plain pandas/dict payload pulled from yfinance, isolated so tests stub without network."""

    info: dict[str, Any] = field(default_factory=dict)
    dividends: pd.Series | None = None
    shares: pd.Series | None = None
    #: (freq, statement) -> DataFrame whose columns are period-end dates, index line items.
    statements: dict[tuple[str, str], pd.DataFrame] = field(default_factory=dict)
    earnings_dates: pd.DataFrame | None = None
    splits: pd.Series | None = None
    #: ``Ticker.calendar``; None when it was not fetched (ETF) or the call failed.
    calendar: dict[str, Any] | None = None


def _is_etf(info: dict[str, Any]) -> bool:
    return str(info.get("quoteType", "")).upper() == "ETF"


def _download_fundamentals(symbol: str) -> RawFundamentals:
    """One symbol's worth of yfinance corporate data. Throttled before each distinct call and
    each call bounded by a wall-clock ceiling (yfinance has no timeout of its own)."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)

    def _call(label: str, work: Any) -> Any:
        throttle()
        return run_within_timeout(work, timeout=DOWNLOAD_TIMEOUT_SECONDS, label=f"{symbol}:{label}")

    info = _call("info", lambda: ticker.info) or {}
    dividends = _call("dividends", lambda: ticker.dividends)
    shares = _call("shares", lambda: ticker.get_shares_full(start="2000-01-01"))
    splits = _call("splits", lambda: ticker.splits)

    raw = RawFundamentals(info=info, dividends=dividends, shares=shares, splits=splits)
    if _is_etf(info):
        # ETFs serve no statements, earnings dates or calendar — skip those calls entirely.
        return raw

    raw.earnings_dates = _call("earnings", lambda: ticker.get_earnings_dates(limit=40))
    try:
        calendar = _call("calendar", lambda: ticker.calendar)
    except YahooProviderError as exc:
        # Upcoming dividend dates are a side dish: losing them must not cost the statements.
        logger.warning("yahoo calendar for %s unavailable: %s", symbol, exc)
        calendar = None
    raw.calendar = calendar if isinstance(calendar, dict) else None
    for (freq, statement), attr in _STATEMENT_ATTRS.items():
        frame = _call(f"{freq}-{statement}", lambda attr=attr: getattr(ticker, attr))
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            raw.statements[(freq, statement)] = frame
    return raw


def _to_date(value: Any) -> date | None:
    """Coerce a pandas Timestamp / datetime / date to a plain ``date`` (None if not usable)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    return ts.date()


def _to_utc(value: Any) -> datetime | None:
    """Coerce a (possibly tz-aware) pandas Timestamp to a UTC datetime."""
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    ts = ts.tz_localize(UTC) if ts.tzinfo is None else ts.tz_convert(UTC)
    return ts.to_pydatetime()


def _clean_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _update_instrument_metadata(instrument: Instrument, info: dict[str, Any]) -> None:
    """Refresh current-only metadata; never overwrite an existing value with a blank one."""
    if sector := info.get("sector"):
        instrument.sector = str(sector)
    if industry := info.get("industry"):
        instrument.industry = str(industry)
    if name := (info.get("longName") or info.get("shortName")):
        instrument.name = str(name)
    quote_type = str(info.get("quoteType", "")).upper()
    if quote_type == "ETF":
        instrument.asset_class = "ETF"
    elif quote_type == "EQUITY":
        instrument.asset_class = "STK"


def _upsert_dividends(session: Session, instrument: Instrument, series: pd.Series | None) -> int:
    if series is None or series.empty:
        return 0
    count = 0
    for index, amount in series.items():
        ex_date = _to_date(index)
        value = _clean_float(amount)
        if ex_date is None or value is None:
            continue
        existing = session.scalar(
            select(Dividend).where(
                Dividend.instrument_id == instrument.id,
                Dividend.ex_date == ex_date,
                Dividend.source == SOURCE,
            )
        )
        if existing:
            existing.amount = value
        else:
            session.add(
                Dividend(instrument_id=instrument.id, ex_date=ex_date, amount=value, source=SOURCE)
            )
        count += 1
    return count


def _upsert_share_counts(session: Session, instrument: Instrument, series: pd.Series | None) -> int:
    if series is None or series.empty:
        return 0
    count = 0
    for index, shares in series.items():
        day = _to_date(index)
        value = _clean_float(shares)
        if day is None or value is None:
            continue
        existing = session.scalar(
            select(ShareCount).where(
                ShareCount.instrument_id == instrument.id,
                ShareCount.date == day,
                ShareCount.source == SOURCE,
            )
        )
        if existing:
            existing.shares = value
        else:
            session.add(
                ShareCount(instrument_id=instrument.id, date=day, shares=value, source=SOURCE)
            )
        count += 1
    return count


def _upsert_splits(session: Session, instrument: Instrument, series: pd.Series | None) -> int:
    """Upsert split ratios (new shares per old). yfinance pads with 0 for "no split"."""
    if series is None or series.empty:
        return 0
    count = 0
    for index, ratio in series.items():
        ex_date = _to_date(index)
        value = _clean_float(ratio)
        if ex_date is None or not value:
            continue
        existing = session.scalar(
            select(StockSplit).where(
                StockSplit.instrument_id == instrument.id,
                StockSplit.ex_date == ex_date,
                StockSplit.source == SOURCE,
            )
        )
        if existing:
            existing.ratio = value
        else:
            session.add(
                StockSplit(instrument_id=instrument.id, ex_date=ex_date, ratio=value, source=SOURCE)
            )
        count += 1
    return count


def _upsert_earnings(
    session: Session, instrument: Instrument, frame: pd.DataFrame | None, now: datetime
) -> tuple[int, list[date]]:
    """Upsert earnings events; return (rows upserted, report dates) for point-in-time lagging.

    A non-empty frame is the full upcoming schedule, so an upcoming row it no longer lists
    was re-dated and is deleted.
    """
    report_dates: list[date] = []
    if frame is None or frame.empty:
        return 0, report_dates
    count = 0
    seen: set[tuple[int, datetime]] = set()
    for index, row in frame.iterrows():
        report_ts = _to_utc(index)
        if report_ts is None:
            continue
        report_dates.append(report_ts.date())
        fields = {
            column: _clean_float(row.get(label)) for label, column in _EARNINGS_COLUMNS.items()
        }
        upsert_earnings_event(session, instrument.id, report_ts, SOURCE, fields, now)
        seen.add((instrument.id, report_ts))
        count += 1
    if seen:
        delete_vanished_future_earnings(
            session, source=SOURCE, keep=seen, after=now, instrument_ids=[instrument.id]
        )
    return count, report_dates


def _calendar_dates(value: Any) -> list[date]:
    """``Ticker.calendar`` values are a date or a list of dates; anything else is no date."""
    values = value if isinstance(value, list | tuple) else [value]
    dates = []
    for item in values:
        try:
            day = _to_date(item)
        except (TypeError, ValueError):
            day = None
        if day is not None:
            dates.append(day)
    return dates


def _json_value(value: Any) -> Any:
    """``Ticker.calendar`` values as JSON: dates as ISO strings, NaN as null."""
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, bool | str):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return None if pd.isna(number) else number


def _upsert_corporate_events(
    session: Session, instrument: Instrument, calendar: dict[str, Any] | None, now: datetime
) -> int:
    """Upsert announced dividend dates on or after today; delete future ones now unannounced.

    ``None`` (not fetched, or failed) and ``{}`` (yfinance's own failure value) change nothing.
    """
    if not calendar:
        return 0
    today = now.date()
    events: dict[tuple[str, date], str] = {}
    for key, kind in _CALENDAR_KINDS.items():
        for day in _calendar_dates(calendar.get(key)):
            if day >= today:
                events[(kind, day)] = key
    snapshot = {str(key): _json_value(value) for key, value in calendar.items()}

    for (kind, day), key in events.items():
        raw = {"field": key, "calendar": snapshot}
        existing = session.scalar(
            select(CorporateEvent).where(
                CorporateEvent.instrument_id == instrument.id,
                CorporateEvent.kind == kind,
                CorporateEvent.event_date == day,
                CorporateEvent.source == SOURCE,
            )
        )
        if existing:
            existing.raw = raw
            existing.fetched_at = now
        else:
            session.add(
                CorporateEvent(
                    instrument_id=instrument.id,
                    kind=kind,
                    event_date=day,
                    source=SOURCE,
                    raw=raw,
                    first_seen_at=now,
                    fetched_at=now,
                )
            )

    upcoming = session.scalars(
        select(CorporateEvent).where(
            CorporateEvent.instrument_id == instrument.id,
            CorporateEvent.source == SOURCE,
            CorporateEvent.event_date > today,
        )
    ).all()
    for row in upcoming:
        if (row.kind, row.event_date) not in events:
            session.delete(row)
    return len(events)


def _match_report_date(period_end: date, report_dates: list[date]) -> date | None:
    """Nearest earnings report strictly after ``period_end`` and within ``MAX_REPORT_LAG_DAYS``."""
    horizon = period_end + timedelta(days=MAX_REPORT_LAG_DAYS)
    candidates = [d for d in report_dates if period_end < d <= horizon]
    return min(candidates) if candidates else None


def _statement_payload(frame: pd.DataFrame, period_col: Any) -> dict[str, float]:
    payload: dict[str, float] = {}
    for line_item, value in frame[period_col].items():
        clean = _clean_float(value)
        if clean is not None:
            payload[str(line_item)] = clean
    return payload


def _upsert_statements(
    session: Session,
    instrument: Instrument,
    statements: dict[tuple[str, str], pd.DataFrame],
    report_dates: list[date],
    now: datetime,
) -> int:
    count = 0
    for (freq, statement), frame in statements.items():
        for period_col in frame.columns:
            period_end = _to_date(period_col)
            if period_end is None:
                continue
            payload = _statement_payload(frame, period_col)
            if not payload:
                continue
            report_date = _match_report_date(period_end, report_dates)
            existing = session.scalar(
                select(FundamentalSnapshot).where(
                    FundamentalSnapshot.instrument_id == instrument.id,
                    FundamentalSnapshot.freq == freq,
                    FundamentalSnapshot.statement == statement,
                    FundamentalSnapshot.period_end == period_end,
                )
            )
            if existing:
                existing.payload = payload
                existing.fetched_at = now
                if report_date is not None:
                    existing.report_date = report_date
                # first_seen is deliberately left untouched.
            else:
                session.add(
                    FundamentalSnapshot(
                        instrument_id=instrument.id,
                        freq=freq,
                        statement=statement,
                        period_end=period_end,
                        payload=payload,
                        report_date=report_date,
                        first_seen=now,
                        fetched_at=now,
                    )
                )
            count += 1
    return count


class YahooFundamentalsConnector(Connector):
    name = "yahoo-fundamentals"

    def fetch(self, symbol: str = "", **kwargs) -> int:
        """Upsert every corporate-data kind for one Yahoo symbol; return rows upserted.

        Snapshot-forward: statements are re-upserted each run with ``fetched_at`` refreshed,
        while ``first_seen`` survives from the first insert. ETFs (no statements/earnings/
        calendar) ingest dividends, splits and share counts only, without error.
        """
        yahoo_symbol = symbol.strip().upper()
        if not yahoo_symbol:
            raise ValueError("symbol is required")

        raw = _download_fundamentals(yahoo_symbol)
        now = datetime.now(tz=UTC)

        with self.session() as session:
            instrument = get_or_create_instrument(session, yahoo_symbol)
            _update_instrument_metadata(instrument, raw.info)

            earnings_count, report_dates = _upsert_earnings(
                session, instrument, raw.earnings_dates, now
            )
            count = earnings_count
            count += _upsert_dividends(session, instrument, raw.dividends)
            count += _upsert_splits(session, instrument, raw.splits)
            count += _upsert_corporate_events(session, instrument, raw.calendar, now)
            count += _upsert_share_counts(session, instrument, raw.shares)
            count += _upsert_statements(session, instrument, raw.statements, report_dates, now)
            return count
