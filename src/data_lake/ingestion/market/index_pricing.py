"""Find prices for every index membership span, including members that later died.

For each unresolved span in ``index_memberships`` this tries, in order:

a. bars already stored — a ``(symbol, SMART, USD)`` instrument whose Yahoo bars cover the
   span: resolved with no network call;
b. a Yahoo probe — stored only if the download covers the span, so a dead or reused ticker
   never leaves an empty or wrong instrument behind;
c. Tiingo, for tickers Yahoo has dropped — the span's ticker and its bankruptcy ``Q``
   ticker, each usable only when Tiingo lists it *exactly once*: a reused ticker is listed
   several times, and there is no telling which company the API would serve;
d. a *partial* series from any of the three, when none covers the whole span;
e. otherwise ``unpriced``, retried after ``retry_unpriced_days``.

**No rename is ever guessed** (``ANTM`` -> ``ELV``): a wrong guess prices one company with
another's history, which is worse than a span left visibly unpriced for the consumer's
coverage report. What replaces the guess is ``sp500_renames.csv`` beside this module, a
curated table where every row cites a press release, SEC filing or exchange notice showing
the same legal entity's listing continuing under a new ticker. An acquired company is never
mapped to its acquirer (``STI`` -> ``TFC``): that history is the acquirer's. A span whose
symbol is in the table is priced from the chain's final ticker (``SYMC`` -> ``NLOK`` ->
``GEN``), under exactly the same coverage rules, and its resolution says so
(``yahoo:renamed:GEN``). Only a span that ended by the rename — or, still open, is within
``RENAME_SLACK`` of it, since the index file records a hand-off late — is mapped: one that
runs on past it means the old ticker went on naming an index member, so it was reused, and
that span is left to price as itself.

**Partial pricing** accepts a series that starts late but runs to the span's end and holds
at least ``MIN_PARTIAL_BARS`` bars inside the window (``FOXA`` has Yahoo history only from
the 2019 Fox spin-off, its span from 2004). The consumer skips a member on days with no bars
and counts those member-days as unpriced, so the missing head costs coverage, not
correctness. *Requiring the end is what makes a late start safe*: whatever trades under the
ticker at the end of the span is the company that was in the index then, while a series that
only overlaps the start may be a later company that reused the ticker. So an early-ending
series is never accepted. A partial resolution records where its bars begin
(``yahoo:partial:2019-03-19``, ``tiingo:XYZ:partial:2019-03-19``) behind the same provider
prefix, and is taken only when no source covers the whole span.

One case the end cannot vouch for: providers file history by *security*, not by ticker, so
a series can reach back to before its security took the ticker. ``IR``'s span runs from 2010,
but the security trading as ``IR`` today was Gardner Denver (``GDI``) until 2020-03-02, while
the member under ``IR`` was Ingersoll-Rand plc. The rename table records such hand-offs too,
so a span whose ticker the table shows being taken over *inside* its window is left
``unpriced:ticker-taken:<date>`` with no provider call: whatever series exists would price
its head with the wrong company.
"""

import csv
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from data_lake.db.models import IndexMembership, PriceBar
from data_lake.ingestion.base import SessionFactory, resolve_session_factory
from data_lake.ingestion.market import yahoo, yahoo_common
from data_lake.ingestion.market.bars import upsert_daily_bars
from data_lake.ingestion.market.index_membership import INDEX_CODE
from data_lake.ingestion.market.tiingo import (
    TiingoBudgetExhausted,
    TiingoConnector,
    TiingoListing,
    TiingoProviderError,
    find_instrument,
)
from data_lake.ingestion.market.yahoo_common import YahooProviderError

__all__ = [
    "MIN_PARTIAL_BARS",
    "RENAMES_FILE",
    "PricingOptions",
    "PricingReport",
    "Rename",
    "covers",
    "covers_partially",
    "final_symbol",
    "load_renames",
    "resolve_index_prices",
]

#: Slack on each end of a span: a listing that starts within a month of the window, or bars
#: that stop within ten days of its end, still count as covering it.
START_SLACK = timedelta(days=31)
END_SLACK = timedelta(days=10)

#: The fewest bars inside the window a late-starting series needs to price a span: about a
#: trading year (252 sessions, less a couple for halts and holidays). A year is what the
#: consumer's momentum features need before a name can be entered at all, so a shorter
#: series buys little; and below it is where stray bars live, such as Yahoo's 9-bar daily
#: stub for ``PSKY``.
MIN_PARTIAL_BARS = 250
#: The shortest calendar stretch that can hold ``MIN_PARTIAL_BARS`` weekday bars, for judging
#: a Tiingo listing by its dates before spending budget on it.
_MIN_PARTIAL_SPAN = timedelta(days=MIN_PARTIAL_BARS * 7 // 5)

#: How far past a rename a span of the old symbol may run and still be mapped: the index
#: file can record the hand-off a few weeks late.
RENAME_SLACK = timedelta(days=31)

RENAMES_FILE = Path(__file__).with_name("sp500_renames.csv")


@dataclass
class PricingReport:
    already: int = 0
    resolved_yahoo: int = 0
    resolved_tiingo: int = 0
    unpriced: int = 0
    deferred: int = 0
    #: Of the resolved spans above, those priced only from a late start.
    partial: int = 0
    #: Of the resolved spans above, those priced through the rename table.
    renamed: int = 0
    #: ``"SYMBOL@start"`` -> error, for spans whose provider call raised.
    failed: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Rename:
    """One row of the curated rename table: ``old_symbol`` continued as ``new_symbol``."""

    old_symbol: str
    new_symbol: str
    effective_date: date
    source: str


def load_renames(path: Path = RENAMES_FILE) -> dict[str, Rename]:
    """The rename table keyed by old symbol, refusing any row that would make it ambiguous.

    Raises ``ValueError`` on a row without a source, a repeated old symbol, a self-rename or
    a chain that loops — each would turn the table into the guess it exists to replace.
    """
    renames: dict[str, Rename] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for line, row in enumerate(csv.DictReader(handle), start=2):
            values = {k: (row.get(k) or "").strip() for k in Rename.__dataclass_fields__}
            if not all(values.values()):
                raise ValueError(f"{path.name}:{line}: every column needs a value: {row}")
            rename = Rename(
                old_symbol=values["old_symbol"],
                new_symbol=values["new_symbol"],
                effective_date=date.fromisoformat(values["effective_date"]),
                source=values["source"],
            )
            if rename.old_symbol == rename.new_symbol:
                raise ValueError(f"{path.name}:{line}: {rename.old_symbol} renamed to itself")
            if rename.old_symbol in renames:
                raise ValueError(f"{path.name}:{line}: {rename.old_symbol} appears twice")
            renames[rename.old_symbol] = rename
    for symbol in renames:
        final_symbol(symbol, renames)  # raises on a cycle
    return renames


def final_symbol(symbol: str, renames: Mapping[str, Rename]) -> str:
    """The ticker ``symbol`` trades under at the end of its rename chain (itself if none)."""
    seen = [symbol]
    while symbol in renames:
        symbol = renames[symbol].new_symbol
        if symbol in seen:
            raise ValueError(f"rename chain loops: {' -> '.join([*seen, symbol])}")
        seen.append(symbol)
    return symbol


@dataclass(frozen=True)
class PricingOptions:
    """The knobs a scheduled run leaves at their defaults.

    ``today`` defaults to the current UTC date. Unpriced spans are retried after
    ``retry_unpriced_days``; bars are fetched from ``warmup_days`` before a span's window.
    ``renames`` defaults to the packaged table (``RENAMES_FILE``).
    """

    index_code: str = INDEX_CODE
    today: date | None = None
    retry_unpriced_days: int = 30
    warmup_days: int = 400
    renames: Mapping[str, Rename] | None = None


@dataclass(frozen=True)
class _Span:
    id: int
    symbol: str
    start: date
    end: date | None


@dataclass(frozen=True)
class _Series:
    """What a source holds for one window: its first and last bar, and the bars inside."""

    first: date
    last: date
    in_window: int


@dataclass(frozen=True)
class _Window:
    """One span's pricing window: ``start..need_to``, fetched from ``fetch_from``.

    ``renamed_to`` is the rename table's final ticker for the span's symbol, when it applies;
    ``ticker_taken_on`` the date inside the window another security took the span's ticker.
    """

    span: _Span
    start: date
    need_to: date
    fetch_from: date
    renamed_to: str | None = None
    ticker_taken_on: date | None = None

    @property
    def label(self) -> str:
        return f"{self.span.symbol}@{self.span.start.isoformat()}"

    @property
    def provider_symbol(self) -> str:
        return _provider_symbol(self.renamed_to or self.span.symbol)

    def fit(self, series: _Series | None) -> "_Fit | None":
        """How ``series`` prices this window: fully, from a late start, or not at all."""
        if series is None:
            return None
        if covers(series.first, series.last, self.start, self.need_to):
            return _Fit(partial_from=None)
        if covers_partially(series.first, series.last, series.in_window, self.start, self.need_to):
            return _Fit(partial_from=series.first)
        return None

    def resolution(self, provider: str, fit: "_Fit") -> str:
        """``provider[:renamed[:SYMBOL]][:partial:DATE]`` — the provider stays the prefix.

        A Tiingo ``provider`` already names its ticker, so a rename adds only the marker.
        """
        parts = [provider]
        if self.renamed_to is not None:
            parts.append("renamed")
            if provider == "yahoo":
                parts.append(self.provider_symbol)
        if fit.partial_from is not None:
            parts += ["partial", fit.partial_from.isoformat()]
        return ":".join(parts)


@dataclass(frozen=True)
class _Fit:
    #: ``None`` when the series covers the whole window.
    partial_from: date | None


@dataclass(frozen=True)
class _Candidate:
    """A source that prices a span, ready to commit — or, if partial, held back in case
    another source covers the whole span."""

    fit: _Fit
    #: The resolution's prefix: ``yahoo`` or ``tiingo:TICKER``.
    provider: str
    #: Stores the bars if needed and marks the span; given the session and the resolution.
    commit: Callable[[Session, str], None]
    #: Priced from bars already stored, with no provider call needed.
    stored: bool = False


def covers(first_bar: date, last_bar: date, window_start: date, need_to: date) -> bool:
    """Whether a series from ``first_bar`` to ``last_bar`` prices ``window_start..need_to``."""
    return first_bar <= window_start + START_SLACK and last_bar >= need_to - END_SLACK


def covers_partially(
    first_bar: date, last_bar: date, bars_in_window: int, window_start: date, need_to: date
) -> bool:
    """Whether a series that starts late still prices ``window_start..need_to`` from its start.

    It must reach the end (see the module docstring for why that makes a late start safe)
    and hold ``MIN_PARTIAL_BARS`` bars inside the window. A series that also starts in time
    is ``covers``'s case, not this one.
    """
    return (
        first_bar > window_start + START_SLACK
        and last_bar >= need_to - END_SLACK
        and bars_in_window >= MIN_PARTIAL_BARS
    )


def _provider_symbol(symbol: str) -> str:
    """Yahoo and Tiingo spell class shares with a dash (``BRK-B``), the index file a dot."""
    return symbol.replace(".", "-")


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)  # SQLite hands back naive UTC


def _stored_series(
    session: Session, instrument_id: int, source: str, window: _Window
) -> _Series | None:
    inside = PriceBar.ts.between(
        yahoo_common.daily_ts(window.start), yahoo_common.daily_ts(window.need_to)
    )
    first, last, in_window = session.execute(
        select(func.min(PriceBar.ts), func.max(PriceBar.ts), func.count(case((inside, 1)))).where(
            PriceBar.instrument_id == instrument_id,
            PriceBar.source == source,
            PriceBar.what_to_show == "ADJUSTED_LAST",
            PriceBar.bar_size == "1 day",
        )
    ).one()
    return None if first is None else _Series(first.date(), last.date(), in_window)


def _values_series(values: list[dict[str, Any]], window: _Window) -> _Series | None:
    if not values:
        return None
    days = [value["ts"].date() for value in values]
    in_window = sum(window.start <= day <= window.need_to for day in days)
    return _Series(min(days), max(days), in_window)


def _ticker_missing(error: BaseException | None) -> bool:
    """Yahoo's "possibly delisted" — an answer (no data), not an outage."""
    from yfinance.exceptions import YFTickerMissingError

    return isinstance(error, YFTickerMissingError)


def _probe_yahoo(symbol: str, start: date, need_to: date) -> list[dict[str, Any]]:
    yahoo_common.throttle()
    try:
        frame = yahoo._download_within_timeout(
            symbol,
            start,
            need_to + timedelta(days=1),  # yfinance's `end` is exclusive
            auto_adjust=True,
            timeout=yahoo_common.DOWNLOAD_TIMEOUT_SECONDS,
        )
    except YahooProviderError as exc:
        if _ticker_missing(exc.__cause__):
            return []
        raise
    return yahoo._bar_values(frame)


def _usable_listings(
    symbol: str, listings: dict[str, list[TiingoListing]], window_start: date, need_to: date
) -> list[TiingoListing]:
    """Tiingo tickers that can price the span: the symbol, then its ``Q`` bankruptcy ticker.

    A listing qualifies when its dates could cover the span, or could price it partially:
    reaching the end, with room for ``MIN_PARTIAL_BARS`` inside the window. Those that could
    cover the whole span come first.
    """
    full, partial = [], []
    base = _provider_symbol(symbol)
    for candidate in (base, base + "Q"):
        rows = listings.get(candidate, [])
        if len(rows) != 1 or rows[0].asset_type != "Stock":
            continue  # absent, or a reused ticker we cannot disambiguate
        row = rows[0]
        end = row.end or need_to
        if covers(row.start, end, window_start, need_to):
            full.append(row)
        elif end >= need_to - END_SLACK and max(row.start, window_start) <= (
            need_to - _MIN_PARTIAL_SPAN
        ):
            partial.append(row)
    return full + partial


def _mark(
    session: Session,
    span_id: int,
    instrument_id: int | None,
    resolution: str,
    now: datetime,
) -> None:
    row = session.get(IndexMembership, span_id)
    assert row is not None
    row.instrument_id = instrument_id
    row.resolution = resolution
    row.resolved_at = now


def _pending_spans(
    factory: SessionFactory, since: date, options: PricingOptions, now: datetime
) -> list[_Span]:
    """Unresolved spans live on or after ``since``, minus those marked unpriced too recently."""
    retry_after = now - timedelta(days=options.retry_unpriced_days)
    with factory() as session:
        rows = session.scalars(
            select(IndexMembership)
            .where(
                IndexMembership.index_code == options.index_code,
                IndexMembership.instrument_id.is_(None),
                or_(IndexMembership.end_date.is_(None), IndexMembership.end_date >= since),
            )
            .order_by(IndexMembership.symbol, IndexMembership.start_date)
        ).all()
        return [
            _Span(row.id, row.symbol, row.start_date, row.end_date)
            for row in rows
            if not (
                (row.resolution or "").split(":")[0] == "unpriced"
                and row.resolved_at is not None
                and _aware(row.resolved_at) > retry_after
            )
        ]


def _renamed_to(symbol: str, need_to: date, renames: Mapping[str, Rename]) -> str | None:
    """The span's final ticker, if the table renames its symbol and the span ended by then."""
    rename = renames.get(symbol)
    if rename is None or need_to > rename.effective_date + RENAME_SLACK:
        return None
    return final_symbol(symbol, renames)


def _ticker_taken_on(
    symbol: str, start: date, need_to: date, renames: Mapping[str, Rename]
) -> date | None:
    """When, inside ``start..need_to``, the table shows another security taking ``symbol``."""
    for rename in renames.values():
        if rename.new_symbol == symbol and start + START_SLACK < rename.effective_date <= need_to:
            return rename.effective_date
    return None


def _window(
    span: _Span, since: date, today: date, warmup_days: int, renames: Mapping[str, Rename]
) -> _Window:
    start = max(span.start, since)
    need_to = span.end or today
    return _Window(
        span,
        start,
        need_to,
        start - timedelta(days=warmup_days),
        _renamed_to(span.symbol, need_to, renames),
        _ticker_taken_on(span.symbol, start, need_to, renames),
    )


@dataclass
class _Run:
    """One run's state: what is left of the Yahoo budget, and whether Tiingo is blocked."""

    factory: SessionFactory
    now: datetime
    yahoo_left: int
    tiingo: TiingoConnector | None
    listings: dict[str, list[TiingoListing]]
    #: Budget spent or key refused: defer rather than mark unpriced.
    tiingo_blocked: bool = False
    attempted: int = 0
    report: PricingReport = field(default_factory=PricingReport)

    def resolve(self, window: _Window) -> None:
        if window.ticker_taken_on is not None:
            with self.factory() as session:
                resolution = f"unpriced:ticker-taken:{window.ticker_taken_on.isoformat()}"
                _mark(session, window.span.id, None, resolution, self.now)
            self.report.unpriced += 1
            return
        stored = self._stored(window)
        if stored is not None and stored.fit.partial_from is None:
            self._commit(window, stored)
            return
        if self.yahoo_left <= 0:
            self.report.deferred += 1
            return
        self.yahoo_left -= 1
        self.attempted += 1
        try:
            self._resolve_with_providers(window, [stored] if stored else [])
        except TiingoProviderError as exc:
            if exc.status in (401, 403):
                self.tiingo_blocked = True  # the key will not start working mid-run
            self.report.failed[window.label] = str(exc)
        except YahooProviderError as exc:
            self.report.failed[window.label] = str(exc)

    def raise_if_all_failed(self) -> None:
        failed = self.report.failed
        if self.attempted and len(failed) == self.attempted:
            sample = "; ".join(f"{k}: {v}" for k, v in list(failed.items())[:3])
            raise RuntimeError(f"all {self.attempted} span(s) sent to a provider failed ({sample})")

    def _commit(self, window: _Window, candidate: _Candidate) -> None:
        with self.factory() as session:
            candidate.commit(session, window.resolution(candidate.provider, candidate.fit))
        if candidate.stored:
            self.report.already += 1
        elif candidate.provider == "yahoo":
            self.report.resolved_yahoo += 1
        else:
            self.report.resolved_tiingo += 1
        if candidate.fit.partial_from is not None:
            self.report.partial += 1
        if window.renamed_to is not None:
            self.report.renamed += 1

    def _stored(self, window: _Window) -> _Candidate | None:
        with self.factory() as session:
            instrument = yahoo_common.get_instrument(session, window.provider_symbol)
            if instrument is None:
                return None
            instrument_id = instrument.id
            fit = window.fit(_stored_series(session, instrument_id, "yahoo", window))
        if fit is None:
            return None

        def commit(session: Session, resolution: str) -> None:
            _mark(session, window.span.id, instrument_id, resolution, self.now)

        return _Candidate(fit, "yahoo", commit, stored=True)

    def _resolve_with_providers(self, window: _Window, partials: list[_Candidate]) -> None:
        yahoo_candidate = self._try_yahoo(window)
        if yahoo_candidate is not None:
            if yahoo_candidate.fit.partial_from is None:
                self._commit(window, yahoo_candidate)
                return
            partials.insert(0, yahoo_candidate)  # fresher than the stored bars it overlaps
        if self.tiingo_blocked:
            self.report.deferred += 1
            return
        tiingo_candidate = self._try_tiingo(window, have_partial=bool(partials))
        if tiingo_candidate is not None:
            if tiingo_candidate.fit.partial_from is None:
                self._commit(window, tiingo_candidate)
                return
            partials.append(tiingo_candidate)
        if self.tiingo_blocked:  # spent while trying this span; it may yet cover in full
            self.report.deferred += 1
            return
        if partials:
            self._commit(window, min(partials, key=lambda c: c.fit.partial_from or date.min))
            return
        with self.factory() as session:
            _mark(session, window.span.id, None, "unpriced", self.now)
        self.report.unpriced += 1

    def _try_yahoo(self, window: _Window) -> _Candidate | None:
        values = _probe_yahoo(window.provider_symbol, window.fetch_from, window.need_to)
        fit = window.fit(_values_series(values, window))
        if fit is None:
            return None

        def commit(session: Session, resolution: str) -> None:
            instrument = yahoo_common.get_or_create_instrument(session, window.provider_symbol)
            upsert_daily_bars(session, instrument.id, values, source="yahoo")
            _mark(session, window.span.id, instrument.id, resolution, self.now)

        return _Candidate(fit, "yahoo", commit)

    def _try_tiingo(self, window: _Window, *, have_partial: bool) -> _Candidate | None:
        """The first Tiingo listing that covers the span, else the first that prices it partially.

        A listing that could only ever be partial is not worth Tiingo's budget when another
        source already prices the span partially.
        """
        if self.tiingo is None:
            return None
        partial: _Candidate | None = None
        usable = _usable_listings(
            window.provider_symbol, self.listings, window.start, window.need_to
        )
        for listing in usable:
            could_cover = covers(
                listing.start, listing.end or window.need_to, window.start, window.need_to
            )
            if not could_cover and (have_partial or partial is not None):
                continue
            try:
                self.tiingo.fetch(
                    symbol=listing.ticker,
                    date_from=window.fetch_from.isoformat(),
                    date_to=window.need_to.isoformat(),
                    exchange=listing.exchange,
                )
            except TiingoBudgetExhausted:
                self.tiingo_blocked = True  # the caller defers the span
                return None
            candidate = self._tiingo_candidate(window, listing)
            if candidate is not None and candidate.fit.partial_from is None:
                return candidate
            if candidate is not None and partial is None:
                partial = candidate
        return partial

    def _tiingo_candidate(self, window: _Window, listing: TiingoListing) -> _Candidate | None:
        with self.factory() as session:
            stored = find_instrument(session, listing.ticker, listing.exchange)
            if stored is None:
                return None
            instrument_id = stored.id
            fit = window.fit(_stored_series(session, instrument_id, "tiingo", window))
        if fit is None:
            return None

        def commit(session: Session, resolution: str) -> None:
            _mark(session, window.span.id, instrument_id, resolution, self.now)

        return _Candidate(fit, f"tiingo:{listing.ticker}", commit)


def resolve_index_prices(
    *,
    since: date,
    max_yahoo: int = 100,
    tiingo: TiingoConnector | None = None,
    tiingo_listings: dict[str, list[TiingoListing]] | None = None,
    options: PricingOptions | None = None,
    session_factory: SessionFactory | None = None,
) -> PricingReport:
    """Resolve prices for the index's spans live on or after ``since``.

    Bars are fetched from ``options.warmup_days`` before the span's window, since momentum
    features need about a year of history before a name can be entered. At most
    ``max_yahoo`` Yahoo probes run; spans past that, or needing Tiingo once its budget is
    spent, are *deferred* (left for the next run) rather than marked unpriced. Tiingo is
    tried only when both ``tiingo`` and ``tiingo_listings`` are given.

    Per-span provider errors land in ``report.failed`` and the run continues. If every span
    that reached a provider failed, raises ``RuntimeError`` — so a scheduler can tell an
    outage from a quiet run.
    """
    factory = resolve_session_factory(session_factory)
    options = options or PricingOptions()
    renames = load_renames() if options.renames is None else options.renames
    today = options.today or _utcnow().date()
    now = _utcnow()
    run = _Run(
        factory=factory,
        now=now,
        yahoo_left=max_yahoo,
        tiingo=tiingo if tiingo_listings is not None else None,
        listings=tiingo_listings or {},
    )
    for span in _pending_spans(factory, since, options, now):
        run.resolve(_window(span, since, today, options.warmup_days, renames))
    run.raise_if_all_failed()
    return run.report
