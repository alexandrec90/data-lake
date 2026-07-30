"""Lake-side tables: market, corporate, news, social and derived-feature data.

These are the datasets the shared lake is allowed to hold: non-PII, non-account, reusable by a
foreign consumer. Account-shaped tables — orders, executions, predictions, backtest runs, risk
state — belong to the consumer and are declared in *its* repo against this ``Base``, so the
dependency runs consumer → lake and never the reverse. Adding such a table here would ship a
consumer's audit trail to every other consumer of the package; ``tests/test_lake_seam.py``
enforces that mechanically.

Conventions:
- Timestamps are timezone-aware UTC.
- External payloads are preserved in a `raw` JSON column for reprocessing.
- Upsert keys: (source, external_id) for text content; (instrument, ts, bar_size, source,
  what_to_show) for bars.
- Privacy (Québec Law 25): social authors are stored as
  hashes, never usernames.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
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


class EarningsEvent(Base):
    """Earnings report timestamps (yfinance `get_earnings_dates`, back to ~2001).

    Used to lag statements to their real availability date (point-in-time correctness).
    """

    __tablename__ = "earnings_events"
    __table_args__ = (UniqueConstraint("instrument_id", "report_ts", "source"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    report_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32))  # yahoo


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


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        Index("ix_social_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))  # reddit
    channel: Mapped[str] = mapped_column(String(64))  # subreddit name
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


class TrendPoint(Base):
    """Google Trends interest-over-time samples."""

    __tablename__ = "trend_points"
    __table_args__ = (UniqueConstraint("keyword", "geo", "ts"),)

    id: Mapped[int] = mapped_column(SqliteFriendlyBigInt, primary_key=True)
    keyword: Mapped[str] = mapped_column(String(128))
    geo: Mapped[str] = mapped_column(String(8), default="")  # "" = worldwide, "CA" = Canada
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interest: Mapped[float] = mapped_column(Float)  # 0-100 relative index
