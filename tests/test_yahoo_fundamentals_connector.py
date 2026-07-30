from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import pandas as pd
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.db.models import (
    Base,
    Dividend,
    EarningsEvent,
    FundamentalSnapshot,
    Instrument,
    ShareCount,
)
from data_lake.ingestion.market import yahoo_fundamentals as yf_fund
from data_lake.ingestion.market.yahoo_fundamentals import RawFundamentals


def _make_session_scope():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope() -> Iterator[Session]:
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return scope


def _stock_raw() -> RawFundamentals:
    """A full equity payload: sector/industry, dividends, shares, earnings, statements."""
    dividends = pd.Series(
        [0.5, 0.6],
        index=pd.DatetimeIndex(["2023-02-10", "2023-05-10"], tz="America/New_York"),
        name="Dividends",
    )
    shares = pd.Series(
        [1_000.0, 1_100.0],
        index=pd.DatetimeIndex(["2023-01-01", "2023-06-01"]),
    )
    # 2022-12-31 is reported on 2023-01-25 (25 days later → matches); 2021-12-31 has no
    # earnings event within 120 days → report_date stays None.
    earnings = pd.DataFrame(
        {"EPS Estimate": [1.2, 1.1]},
        index=pd.DatetimeIndex(["2023-01-25 16:00", "2022-10-26 16:00"], tz="America/New_York"),
    )
    income = pd.DataFrame(
        {
            pd.Timestamp("2022-12-31"): [100.0, 20.0],
            pd.Timestamp("2021-12-31"): [90.0, 15.0],
        },
        index=["Total Revenue", "Net Income"],
    )
    return RawFundamentals(
        info={
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "longName": "Apple Inc.",
            "quoteType": "EQUITY",
        },
        dividends=dividends,
        shares=shares,
        statements={("annual", "income"): income},
        earnings_dates=earnings,
    )


def _fetch(monkeypatch, session_cm, raw: RawFundamentals, symbol: str = "AAPL") -> int:
    monkeypatch.setattr(yf_fund, "_download_fundamentals", lambda s: raw)
    return yf_fund.YahooFundamentalsConnector(session_factory=session_cm).fetch(symbol=symbol)


def test_fetch_upserts_all_kinds_and_metadata(monkeypatch):
    session_cm = _make_session_scope()
    count = _fetch(monkeypatch, session_cm, _stock_raw())

    # 2 earnings + 2 dividends + 2 shares + 2 statement periods
    assert count == 8
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "AAPL"))
        assert instrument is not None
        assert instrument.exchange == "SMART"
        assert instrument.sector == "Technology"
        assert instrument.industry == "Consumer Electronics"
        assert instrument.name == "Apple Inc."
        assert instrument.asset_class == "STK"

        divs = session.scalars(select(Dividend).order_by(Dividend.ex_date)).all()
        assert [d.ex_date for d in divs] == [date(2023, 2, 10), date(2023, 5, 10)]
        assert divs[0].amount == 0.5
        assert divs[0].source == "yahoo"

        shares = session.scalars(select(ShareCount).order_by(ShareCount.date)).all()
        assert [s.date for s in shares] == [date(2023, 1, 1), date(2023, 6, 1)]
        assert shares[1].shares == 1_100.0

        events = session.scalars(select(EarningsEvent)).all()
        assert len(events) == 2

        snaps = session.scalars(
            select(FundamentalSnapshot).order_by(FundamentalSnapshot.period_end)
        ).all()
        assert len(snaps) == 2
        assert snaps[0].period_end == date(2021, 12, 31)
        assert snaps[0].report_date is None  # no earnings event within 120 days
        assert snaps[1].period_end == date(2022, 12, 31)
        assert snaps[1].report_date == date(2023, 1, 25)  # matched earnings event
        assert snaps[1].payload == {"Total Revenue": 100.0, "Net Income": 20.0}
        assert snaps[1].freq == "annual"
        assert snaps[1].statement == "income"


def test_refetch_is_idempotent_and_first_seen_survives(monkeypatch):
    session_cm = _make_session_scope()

    assert _fetch(monkeypatch, session_cm, _stock_raw()) == 8
    with session_cm() as session:
        snap = session.scalar(
            select(FundamentalSnapshot).where(FundamentalSnapshot.period_end == date(2022, 12, 31))
        )
        first_seen = snap.first_seen
        original_fetched = snap.fetched_at

    # Second run must not duplicate any rows and must preserve first_seen.
    assert _fetch(monkeypatch, session_cm, _stock_raw()) == 8
    with session_cm() as session:
        assert len(session.scalars(select(Dividend)).all()) == 2
        assert len(session.scalars(select(ShareCount)).all()) == 2
        assert len(session.scalars(select(EarningsEvent)).all()) == 2
        assert len(session.scalars(select(FundamentalSnapshot)).all()) == 2

        snap = session.scalar(
            select(FundamentalSnapshot).where(FundamentalSnapshot.period_end == date(2022, 12, 31))
        )
        assert snap.first_seen == first_seen  # never updated
        assert snap.fetched_at >= original_fetched  # refreshed each run


def test_etf_skips_statements_gracefully(monkeypatch):
    session_cm = _make_session_scope()
    etf_raw = RawFundamentals(
        info={"longName": "iShares Core Equity ETF", "quoteType": "ETF"},
        dividends=pd.Series(
            [0.25],
            index=pd.DatetimeIndex(["2023-03-20"], tz="America/Toronto"),
            name="Dividends",
        ),
        shares=None,
        statements={},
        earnings_dates=None,
    )

    count = _fetch(monkeypatch, session_cm, etf_raw, symbol="XEQT.TO")

    assert count == 1  # dividends only
    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "XEQT"))
        assert instrument is not None
        assert instrument.exchange == "TSX"
        assert instrument.asset_class == "ETF"
        assert len(session.scalars(select(Dividend)).all()) == 1
        assert session.scalars(select(FundamentalSnapshot)).all() == []
        assert session.scalars(select(EarningsEvent)).all() == []


def test_match_report_date_prefers_nearest_within_horizon():
    reports = [date(2023, 1, 25), date(2023, 4, 20), date(2022, 10, 26)]
    # 2022-12-31 → nearest report after it within 120 days is 2023-01-25.
    assert yf_fund._match_report_date(date(2022, 12, 31), reports) == date(2023, 1, 25)
    # A period with no report inside the horizon stays unmatched.
    assert yf_fund._match_report_date(date(2020, 12, 31), reports) is None


def test_metadata_does_not_overwrite_with_blank(monkeypatch):
    session_cm = _make_session_scope()
    _fetch(monkeypatch, session_cm, _stock_raw())

    # A later run whose info lacks sector must not wipe the previously stored value.
    sparse = _stock_raw()
    sparse.info = {"quoteType": "EQUITY"}
    _fetch(monkeypatch, session_cm, sparse)

    with session_cm() as session:
        instrument = session.scalar(select(Instrument).where(Instrument.symbol == "AAPL"))
        assert instrument.sector == "Technology"
        assert instrument.industry == "Consumer Electronics"
