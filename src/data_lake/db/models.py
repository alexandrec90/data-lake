"""Lake-side tables — every domain the lake holds, not one of them.

The market/corporate/news/social tables came first because ``ibkr_trader`` was the first
consumer, and that is the *only* reason they dominate this module. **The lake is not a
finance package.** A rental-listing table, a sports-fixture table, anything else a consumer
wants to share: all are equally at home here, and nothing in the seam tests says otherwise.

What the boundary actually is: **nothing account-shaped.** Orders, executions, predictions,
backtest runs, risk state — a consumer's audit trail — belong in *its* repo, declared against
this ``Base``, so the dependency runs consumer → lake and never the reverse. Declaring such a
table here would ship one consumer's private records to every other consumer of the package.
``tests/test_lake_seam.py`` enforces that mechanically, by table name and by column shape.

The question to ask of a new table is therefore "would a foreign consumer be harmed or
confused by receiving this?", not "is this the same subject as the tables above it".

Conventions:
- Timestamps are timezone-aware UTC.
- External payloads are preserved in a `raw` JSON column for reprocessing, so a parser fix
  never requires re-fetching.
- Upsert keys: (source, external_id) for text content; (platform, external_id) for scraped
  listings; (instrument, ts, bar_size, source, what_to_show) for bars; (index_code, symbol,
  start_date, source) for index membership spans; (source, external_id) for IPO events,
  scheduled economic events and SEC XBRL facts; (source, series_id, date) for macro series;
  (instrument, kind, event_date, source) for announced corporate events.
- ``first_seen_at`` is written on insert and never updated: it is the point-in-time "when did
  we know" hook, distinct from whatever date the provider attaches to the record.
- Privacy (Québec Law 25): people are stored as hashes, never names — social authors and
  listing sellers alike.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from data_lake.db.base import Base, JsonVariant, SqliteFriendlyBigInt


class Instrument(Base):
    """Canonical tradable instrument; maps provider-specific symbols to one row."""

    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("symbol", "exchange", "currency"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    exchange: Mapped[str] = mapped_column(String(32), default="SMART")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    sec_type: Mapped[str] = mapped_column(String(8), default="STK")
    ibkr_con_id: Mapped[int | None] = mapped_column(BigInteger)  # cached from qualifyContracts
    name: Mapped[str | None] = mapped_column(String(256))
    # Eligibility metadata (signals.eligibility): IBKR sec_type is "STK" for both stocks and
    # ETFs, so asset_class distinguishes them; `leveraged` flags leveraged/inverse/volatility
    # ETPs excluded from registered-account trading.
    asset_class: Mapped[str | None] = mapped_column(String(8))  # "STK" | "ETF"
    leveraged: Mapped[bool | None] = mapped_column(Boolean)
    # Corporate metadata (ingestion.market.yahoo_fundamentals): current-only from yfinance
    # `.info`, refreshed on each fundamentals ingest. Static-ish; not a point-in-time record.
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    #: The earliest date this security is known to have traded, under any ticker: a lower
    #: bound on its age, None when unknown. Written by ``ingestion.market.listing_dates`` and
    #: ``ingestion.market.ipo_pricing``, only ever moved *earlier*. Too late is the harmful
    #: error — an old company would pass for a fresh listing — so a source that might be late
    #: is not used at all, and the value is never later than the instrument's first stored bar.
    listed_on: Mapped[date | None] = mapped_column(Date)


class PriceBar(Base):
    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("instrument_id", "ts", "bar_size", "source", "what_to_show"),
        Index("ix_price_bars_instrument_ts", "instrument_id", "ts"),
    )

    # ORM primary key is ``id`` alone. On Postgres the TimescaleDB migration
    # (f7b8c9d0e1f2) replaces this with a composite PRIMARY KEY (id, ts) because a hypertable
    # requires the partitioning column in every unique/PK constraint. That divergence is
    # deliberate and safe: ``id`` stays globally unique via its sequence, ORM UPDATEs still
    # target rows by ``id``, Alembic autogenerate does not diff primary keys, and SQLite (tests)
    # keeps the single-column integer PK it needs for autoincrement.
    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bar_size: Mapped[str] = mapped_column(String(16))  # e.g. "1 day", "1 min"
    source: Mapped[str] = mapped_column(String(32))  # ibkr | alpha_vantage | fmp | finnhub
    what_to_show: Mapped[str] = mapped_column(String(32), default="TRADES")  # or ADJUSTED_LAST…
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)


class IndexMembership(Base):
    """One span of one symbol's membership in a stock index (point-in-time universe).

    Written by ``ingestion.market.index_membership`` from a public reference file; the
    pricing columns are filled by ``ingestion.market.index_pricing``. ``symbol`` is the ticker
    *as spelled during the span* — a rename is a separate span, never rewritten in place, and
    no rename is ever guessed, because pricing one company with another's history is worse
    than leaving a span visibly unpriced.

    Pricing columns: ``instrument_id`` is the instrument whose bars price the span (None until
    resolved); ``resolution`` says how it was found (``yahoo`` | ``tiingo:<ticker>`` |
    ``unpriced``); ``resolved_at`` is when, so an ``unpriced`` span is retried after a while.
    """

    __tablename__ = "index_memberships"
    __table_args__ = (
        UniqueConstraint("index_code", "symbol", "start_date", "source"),
        Index("ix_index_memberships_window", "index_code", "start_date", "end_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    index_code: Mapped[str] = mapped_column(String(32))  # SP500
    symbol: Mapped[str] = mapped_column(String(32))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)  # None = current member
    source: Mapped[str] = mapped_column(String(32))  # fja05680
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    resolution: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Dividend(Base):
    """Cash dividends per instrument (yfinance `Ticker.dividends`, decades deep)."""

    __tablename__ = "dividends"
    __table_args__ = (UniqueConstraint("instrument_id", "ex_date", "source"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ex_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))  # yahoo


class ShareCount(Base):
    """Historical shares outstanding (yfinance `get_shares_full`, ~2015+) for market cap."""

    __tablename__ = "share_counts"
    __table_args__ = (UniqueConstraint("instrument_id", "date", "source"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    date: Mapped[date] = mapped_column(Date)
    shares: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))  # yahoo


class FundamentalSnapshot(Base):
    """One financial statement for one period, snapshotted forward.

    yfinance serves only ~4-5 annual / ~5-7 quarterly periods, so we upsert the latest each
    run. `first_seen` records when a figure first entered our DB and is **never updated** —
    with `report_date` (inferred from earnings dates) it lets feature builds honestly answer
    "what did we know at time t?".
    """

    __tablename__ = "fundamental_snapshots"
    __table_args__ = (UniqueConstraint("instrument_id", "freq", "statement", "period_end"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    freq: Mapped[str] = mapped_column(String(16))  # annual | quarterly
    statement: Mapped[str] = mapped_column(String(16))  # income | balance | cashflow
    period_end: Mapped[date] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JsonVariant)  # line-item name -> value
    report_date: Mapped[date | None] = mapped_column(Date)  # from earnings dates when matchable
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # set on insert only
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # updated each refresh


class StockSplit(Base):
    """Stock splits per instrument (yfinance `Ticker.splits`).

    ``ratio`` is new shares per old share: a 4-for-1 split is ``4.0``, a 1-for-10 reverse
    split ``0.1``.
    """

    __tablename__ = "stock_splits"
    __table_args__ = (UniqueConstraint("instrument_id", "ex_date", "source"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ex_date: Mapped[date] = mapped_column(Date)
    ratio: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32))  # yahoo


class EarningsEvent(Base):
    """Earnings report timestamps, with the estimate and the result when the provider has them.

    Two sources, kept as separate rows: ``yahoo`` (yfinance `get_earnings_dates`, back to
    ~2001, ``report_ts`` carrying the announcement time) and ``finnhub`` (the earnings
    calendar, ``report_ts`` the date at 00:00 UTC with ``hour`` saying ``bmo``/``amc``/``dmh``).

    Used to lag statements to their real availability date (point-in-time correctness). Both
    sources list **upcoming** reports too, with null actuals; a re-dated report is deleted at
    its old timestamp rather than left behind as a ghost. ``first_seen_at`` is null on rows
    written before the column existed.
    """

    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("instrument_id", "report_ts", "source"),
        Index("ix_earnings_events_report_ts", "report_ts"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    report_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32))  # yahoo | finnhub
    eps_estimate: Mapped[float | None] = mapped_column(Float)
    eps_actual: Mapped[float | None] = mapped_column(Float)
    surprise_pct: Mapped[float | None] = mapped_column(Float)
    revenue_estimate: Mapped[float | None] = mapped_column(Float)
    revenue_actual: Mapped[float | None] = mapped_column(Float)
    hour: Mapped[str | None] = mapped_column(String(8))  # bmo | amc | dmh
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CorporateEvent(Base):
    """An announced, upcoming per-instrument event (yfinance `Ticker.calendar`).

    ``kind`` is ``ex_dividend`` | ``dividend_payment``. Only dates on or after the fetch day are
    stored — past ex-dates live in ``dividends`` — and a future event the provider stops
    announcing is deleted, so this table answers "what is scheduled", not "what was".
    """

    __tablename__ = "corporate_events"
    __table_args__ = (
        UniqueConstraint("instrument_id", "kind", "event_date", "source"),
        Index("ix_corporate_events_event_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    kind: Mapped[str] = mapped_column(String(32))
    event_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(32))  # yahoo
    raw: Mapped[dict | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MacroObservation(Base):
    """One observation of one macroeconomic series (``ingestion.macro``).

    Sources: ``fred`` (series ids such as ``CPIAUCSL``) and ``bankofcanada`` (Valet series ids
    such as ``V39079``, the policy rate).

    **Latest vintage, not point in time.** A revised figure overwrites ``value`` in place;
    this is not ALFRED's vintage history, so a backtest reading ``value`` sees revisions it
    could not have known. ``first_seen_at`` (never updated) is the point-in-time hook: it is
    when the observation first entered this table.
    """

    __tablename__ = "macro_observations"
    __table_args__ = (
        UniqueConstraint("source", "series_id", "date"),
        Index("ix_macro_observations_series_date", "series_id", "date"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # fred | bankofcanada
    series_id: Mapped[str] = mapped_column(String(64))
    date: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EconomicEvent(Base):
    """One scheduled macroeconomic event: a data release, a rate decision.

    ``kind`` / ``source`` / ``external_id``:

    - ``data_release`` / ``fred`` / ``"{release_id}:{date}"`` — FRED's release calendar,
      ``release_id`` set.
    - ``fomc_meeting`` / ``federalreserve`` / ``"fomc:{decision date}"`` — ``start_date`` is
      day one, ``event_date`` the decision day; ``raw`` carries ``sep`` (a Summary of Economic
      Projections) and ``notation_vote``.
    - ``boc_rate_announcement`` / ``bankofcanada`` / ``"boc:{date}"`` — ``raw`` carries
      ``mpr`` (published with a Monetary Policy Report).

    A future event its source stops listing is deleted (a rescheduled release would otherwise
    leave a ghost at its old date); past events are kept.
    """

    __tablename__ = "economic_events"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_economic_events_event_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # fred | federalreserve | bankofcanada
    external_id: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(String(2))  # US | CA
    event_date: Mapped[date] = mapped_column(Date)  # the release or decision day
    start_date: Mapped[date | None] = mapped_column(Date)  # FOMC day one
    release_id: Mapped[int | None] = mapped_column(Integer)  # FRED release id
    raw: Mapped[dict | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SecFact(Base):
    """One XBRL fact from SEC's company-concept API (``ingestion.market.sec_xbrl``).

    Every fact is kept, including the same period restated by later filings — the accession
    number is part of ``external_id`` (``"{accn}:{taxonomy}:{concept}:{unit}:{start}:{end}"``,
    ``start`` empty for an instant). 10-Q cash-flow values are **year-to-date** as filed, not
    per quarter. ``frame`` is set on only one fact per period (SEC's pick for that calendar
    frame). ``instrument_id`` is null when no instrument carries the filer's ticker.
    """

    __tablename__ = "sec_facts"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_sec_facts_instrument_concept_period", "instrument_id", "concept", "period_end"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # sec_xbrl
    external_id: Mapped[str] = mapped_column(String(256))
    cik: Mapped[str] = mapped_column(String(10))  # zero-padded to 10 digits
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    taxonomy: Mapped[str] = mapped_column(String(16))  # us-gaap
    concept: Mapped[str] = mapped_column(String(128))
    unit: Mapped[str] = mapped_column(String(16))  # USD | shares
    period_start: Mapped[date | None] = mapped_column(Date)  # None for an instant
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(4))  # FY | Q1 | Q2 | Q3
    form: Mapped[str] = mapped_column(String(16))  # 10-K | 10-Q | 8-K ...
    filed: Mapped[date] = mapped_column(Date)
    frame: Mapped[str | None] = mapped_column(String(16))  # e.g. CY2025Q3
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Feature(Base):
    """One instrument's feature snapshot for one day under one feature-set version.

    Written by signals.features.build_daily_features so training (ML-03) and backtests read
    identical inputs; `feature_set_version` pins what a saved model was trained on. `payload`
    is the numeric feature dict plus the categorical `sector` string.
    """

    __tablename__ = "features"
    __table_args__ = (UniqueConstraint("instrument_id", "ts", "feature_set_version"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # as-of day, midnight UTC
    feature_set_version: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JsonVariant)


class NewsArticle(Base):
    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_news_published_at", "published_at"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # newsapi | finnhub
    external_id: Mapped[str] = mapped_column(String(256))  # provider id or URL hash
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    symbols: Mapped[list | None] = mapped_column(JSON)  # extracted tickers
    sentiment: Mapped[float | None] = mapped_column(Float)  # filled by signals stage
    sentiment_model: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IpoEvent(Base):
    """One IPO-shaped event: a registration filing on EDGAR, or a deal on an IPO calendar.

    Two sources describe the same pipeline at different stages, and are kept as separate rows
    rather than merged, because nothing in either reliably joins them before listing:

    - ``sec_edgar`` (``ingestion.market.sec_edgar``) — one row per filing, keyed by accession
      number. ``S-1``/``F-1``/``DRS`` are ``filed``, their ``/A`` amendments ``amended``,
      ``424B4`` ``priced``, ``RW`` ``withdrawn``. These forms are also used for resales, SPACs
      and follow-ons; every one is stored, and ``raw`` carries the SIC code so a consumer can
      filter. ``filed_at`` is EDGAR's "Date Filed" at 00:00 UTC — for a confidential ``DRS``
      that is the submission date, weeks before it became public. ``is_spac`` comes from the
      same SIC: True for 6770 ("blank checks"), False for any other, None when EDGAR gave none.
    - ``finnhub`` (``ingestion.market.finnhub_ipo``) — one row per deal on Finnhub's calendar.
      The calendar gives no id, and a deal's date, symbol and exchange all move as it
      progresses (``exchange`` is null until a deal is scheduled), so the key is a hash of the
      normalised company name alone and a re-dated deal updates in place.

    ``stage`` is one of ``filed`` | ``amended`` | ``expected`` | ``priced`` | ``withdrawn``;
    the provider's own status or form type stays in ``raw``/``form_type``.

    Pricing columns, filled by ``ingestion.market.ipo_pricing`` on ``finnhub`` ``priced`` rows
    and mirroring ``IndexMembership``'s: ``instrument_id`` is the instrument whose bars price
    the listing (None until resolved); ``resolution`` says how (``yahoo`` |
    ``tiingo:<ticker>`` | ``unpriced`` | ``excluded:<reason>``, the last for a row that is not
    a listing of common shares or ADRs — a SPAC, a unit); ``resolved_at`` is when. The IPO
    cohort is the rows with ``instrument_id`` set.
    """

    __tablename__ = "ipo_events"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_ipo_events_expected_date", "expected_date"),
        Index("ix_ipo_events_filed_at", "filed_at"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))  # finnhub | sec_edgar
    external_id: Mapped[str] = mapped_column(String(256))  # accession number | name hash
    company_name: Mapped[str] = mapped_column(Text)  # as the provider spells it
    symbol: Mapped[str | None] = mapped_column(String(16))
    exchange: Mapped[str | None] = mapped_column(String(32))
    cik: Mapped[str | None] = mapped_column(String(10))  # zero-padded to 10 digits
    stage: Mapped[str] = mapped_column(String(16))
    form_type: Mapped[str | None] = mapped_column(String(16))  # EDGAR form, e.g. "S-1/A"
    filed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expected_date: Mapped[date | None] = mapped_column(Date)  # calendar date, not yet listed
    price_low: Mapped[float | None] = mapped_column(Float)
    price_high: Mapped[float | None] = mapped_column(Float)
    shares: Mapped[int | None] = mapped_column(BigInteger)
    deal_value_usd: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str | None] = mapped_column(Text)  # EDGAR filing index page
    is_spac: Mapped[bool | None] = mapped_column(Boolean)  # sec_edgar only; None = unknown
    raw: Mapped[dict | None] = mapped_column(JSON)
    #: Never updated after insert — an alert dedupes on it, and it is the point-in-time
    #: "when did we know" that ``filed_at`` (a regulatory date) is not.
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"))
    resolution: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        Index("ix_social_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))  # reddit | x
    channel: Mapped[str] = mapped_column(String(64))  # subreddit; X: search | timeline
    external_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    author_hash: Mapped[str | None] = mapped_column(String(64))  # sha256, never the username
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    score: Mapped[int | None] = mapped_column(BigInteger)
    num_comments: Mapped[int | None] = mapped_column(BigInteger)
    symbols: Mapped[list | None] = mapped_column(JSON)
    sentiment: Mapped[float | None] = mapped_column(Float)
    sentiment_model: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[dict | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Listing(Base):
    """One scraped rental/classified listing, plus the verdict a consumer reached on it.

    Written by ``apt-finder`` (Kijiji and Facebook Marketplace, Gatineau QC). It lives here
    rather than in that consumer because it is ordinary shared content — scraped public ads,
    nothing account-shaped — and a second consumer looking for housing data should find it
    rather than re-scrape.

    Two columns are deliberately loose, because the *judging* is the consumer's business and
    the *data* is the lake's:

    - ``verdict`` / ``verdict_reasons`` are free-form strings owned by whichever consumer
      wrote the row. The lake does not define the vocabulary; apt-finder uses
      ``match``/``maybe``/``reject`` with machine-readable reason codes, and another consumer
      with different criteria can use its own without a migration here.
    - The appliance columns are tri-state strings (``present``/``absent``/``unknown``), never
      nullable booleans. "The ad says there is no washer" and "the ad never mentions one" are
      different facts, and collapsing them is what makes a filter either miss good units or
      surface ones with only a hookup.

    Privacy (Québec Law 25): ``seller_hash`` is a ``stable_hash`` digest. The seller's name,
    profile URL and phone number are never stored — the hash exists only to spot one landlord
    posting twenty units.
    """

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        Index("ix_listings_verdict_seen", "verdict", "last_seen_at"),
        Index("ix_listings_posted_at", "posted_at"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16))  # kijiji | facebook
    external_id: Mapped[str] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(Text)

    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    # Numeric, not Float: rent is money, and 1499.99 must not become 1499.9899999.
    price_cad: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    #: What the source quoted before normalisation to monthly ("month" | "week" | "day" |
    #: "unknown"). A weekly quote normalised to monthly is still worth flagging — it usually
    #: means a short-term rental rather than a cheap one.
    price_period: Mapped[str | None] = mapped_column(String(16))

    city: Mapped[str | None] = mapped_column(String(128))
    province: Mapped[str | None] = mapped_column(String(8))
    postal_prefix: Mapped[str | None] = mapped_column(String(3))  # forward sortation area
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    distance_km: Mapped[float | None] = mapped_column(Float)

    #: Québec room notation kept as a room count ("4 1/2" -> 4.5). It is what locals search
    #: by, and converting to bedrooms loses information, so both are stored.
    rooms: Mapped[float | None] = mapped_column(Float)
    bedrooms: Mapped[float | None] = mapped_column(Float)
    unit_type: Mapped[str | None] = mapped_column(String(32))
    is_room_rental: Mapped[bool | None] = mapped_column(Boolean)

    washer: Mapped[str | None] = mapped_column(String(8))
    dryer: Mapped[str | None] = mapped_column(String(8))
    fridge: Mapped[str | None] = mapped_column(String(8))
    stove: Mapped[str | None] = mapped_column(String(8))
    dishwasher: Mapped[str | None] = mapped_column(String(8))

    verdict: Mapped[str] = mapped_column(String(8))
    verdict_reasons: Mapped[list | None] = mapped_column(JsonVariant)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Never updated after insert. "How long has this been listed?" is what separates a real
    #: vacancy from a stale repost.
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seller_hash: Mapped[str | None] = mapped_column(String(64))  # sha256, never the name
    raw: Mapped[dict | None] = mapped_column(JsonVariant)


class TrendPoint(Base):
    """Google Trends interest-over-time samples."""

    __tablename__ = "trend_points"
    __table_args__ = (UniqueConstraint("keyword", "geo", "ts"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(128))
    geo: Mapped[str] = mapped_column(String(8), default="")  # "" = worldwide, "CA" = Canada
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interest: Mapped[float] = mapped_column(Float)  # 0-100 relative index
