import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.db.models import (
    Base,
    CorporateEvent,
    Dividend,
    EarningsEvent,
    FundamentalSnapshot,
    Instrument,
    ShareCount,
    StockSplit,
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


NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


@pytest.fixture
def frozen_now(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(yf_fund, "datetime", FrozenDatetime)
    return NOW


def _earnings_frame(rows: list[tuple[str, float | None, float | None, float | None]]):
    """(timestamp, estimate, reported, surprise) rows shaped like ``get_earnings_dates``."""
    index = pd.DatetimeIndex([r[0] for r in rows], tz="America/New_York", name="Earnings Date")
    return pd.DataFrame(
        {
            "EPS Estimate": [r[1] for r in rows],
            "Reported EPS": [r[2] for r in rows],
            "Surprise(%)": [r[3] for r in rows],
        },
        index=index,
    )


def _raw_with(**overrides) -> RawFundamentals:
    raw = RawFundamentals(info={"quoteType": "EQUITY"})
    for name, value in overrides.items():
        setattr(raw, name, value)
    return raw


def _earnings(session_cm) -> list[EarningsEvent]:
    with session_cm() as session:
        return list(session.scalars(select(EarningsEvent).order_by(EarningsEvent.report_ts)))


def test_splits_are_stored_for_etfs_too_and_zero_padding_dropped(monkeypatch):
    session_cm = _make_session_scope()
    splits = pd.Series(
        [4.0, 0.0, 0.1],
        index=pd.DatetimeIndex(["2020-08-31", "2021-01-04", "2024-06-10"], tz="America/New_York"),
        name="Stock Splits",
    )
    etf = RawFundamentals(info={"quoteType": "ETF"}, splits=splits)

    assert _fetch(monkeypatch, session_cm, etf, symbol="XEQT.TO") == 2
    _fetch(monkeypatch, session_cm, etf, symbol="XEQT.TO")  # idempotent

    with session_cm() as session:
        rows = session.scalars(select(StockSplit).order_by(StockSplit.ex_date)).all()
    assert [(r.ex_date, r.ratio, r.source) for r in rows] == [
        (date(2020, 8, 31), 4.0, "yahoo"),
        (date(2024, 6, 10), 0.1, "yahoo"),
    ]


def test_earnings_carry_eps_and_first_seen(monkeypatch, frozen_now):
    session_cm = _make_session_scope()
    frame = _earnings_frame(
        [("2026-10-30 16:00", 1.6, None, None), ("2026-07-30 16:00", 1.4, 1.5, 7.14)]
    )
    _fetch(monkeypatch, session_cm, _raw_with(earnings_dates=frame))

    (upcoming,) = [e for e in _earnings(session_cm) if e.eps_actual is None]
    (reported,) = [e for e in _earnings(session_cm) if e.eps_actual is not None]
    assert (upcoming.eps_estimate, upcoming.surprise_pct) == (1.6, None)
    assert (reported.eps_estimate, reported.eps_actual, reported.surprise_pct) == (1.4, 1.5, 7.14)
    assert reported.report_ts.replace(tzinfo=UTC) == datetime(2026, 7, 30, 20, tzinfo=UTC)
    assert reported.first_seen_at.replace(tzinfo=UTC) == NOW
    assert reported.fetched_at.replace(tzinfo=UTC) == NOW
    assert reported.revenue_estimate is None and reported.hour is None


def test_a_refresh_fills_the_actual_without_blanking_or_moving_first_seen(monkeypatch, frozen_now):
    session_cm = _make_session_scope()
    _fetch(
        monkeypatch,
        session_cm,
        _raw_with(earnings_dates=_earnings_frame([("2026-10-30 16:00", 1.6, None, None)])),
    )
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        session.scalars(select(EarningsEvent)).one().first_seen_at = marker

    _fetch(
        monkeypatch,
        session_cm,
        _raw_with(earnings_dates=_earnings_frame([("2026-10-30 16:00", None, 1.7, 6.2)])),
    )

    [row] = _earnings(session_cm)
    assert (row.eps_estimate, row.eps_actual, row.surprise_pct) == (1.6, 1.7, 6.2)
    assert row.first_seen_at.replace(tzinfo=UTC) == marker


def test_a_redated_upcoming_report_is_deleted_and_the_past_kept(monkeypatch, frozen_now):
    session_cm = _make_session_scope()
    first = _earnings_frame(
        [("2026-10-30 16:00", 1.6, None, None), ("2026-07-30 16:00", 1.4, 1.5, 7.1)]
    )
    _fetch(monkeypatch, session_cm, _raw_with(earnings_dates=first))
    _fetch(monkeypatch, session_cm, _stock_raw(), symbol="MSFT")  # another instrument

    # The report moves to Nov 2, and the past report is no longer in Yahoo's window.
    moved = _earnings_frame([("2026-11-02 08:00", 1.6, None, None)])
    _fetch(monkeypatch, session_cm, _raw_with(earnings_dates=moved))
    # An empty frame (yfinance's failure mode) deletes nothing.
    _fetch(monkeypatch, session_cm, _raw_with(earnings_dates=pd.DataFrame()))

    with session_cm() as session:
        aapl = session.scalar(select(Instrument.id).where(Instrument.symbol == "AAPL"))
    dates = sorted(e.report_ts.date() for e in _earnings(session_cm) if e.instrument_id == aapl)
    assert dates == [date(2026, 7, 30), date(2026, 11, 2)]
    assert len(_earnings(session_cm)) == 4  # MSFT's two untouched


def _calendar(**overrides):
    calendar = {
        "Dividend Date": date(2026, 11, 13),
        "Ex-Dividend Date": date(2026, 11, 10),
        "Earnings Date": [date(2026, 10, 30)],
        "Earnings High": 1.8,
        "Earnings Average": float("nan"),
        "Revenue Average": 102_000_000_000,
    }
    calendar.update(overrides)
    return calendar


def _corporate(session_cm) -> dict[tuple[str, date], CorporateEvent]:
    with session_cm() as session:
        return {(r.kind, r.event_date): r for r in session.scalars(select(CorporateEvent))}


def test_calendar_dividend_dates_are_stored_from_today_on(monkeypatch, frozen_now):
    session_cm = _make_session_scope()
    calendar = _calendar(**{"Ex-Dividend Date": date(2026, 8, 11)})  # past: in `dividends`

    assert _fetch(monkeypatch, session_cm, _raw_with(calendar=calendar)) == 1

    events = _corporate(session_cm)
    assert set(events) == {("dividend_payment", date(2026, 11, 13))}
    payment = events[("dividend_payment", date(2026, 11, 13))]
    assert payment.source == "yahoo"
    assert payment.raw["field"] == "Dividend Date"
    assert payment.raw["calendar"]["Earnings Date"] == ["2026-10-30"]
    assert payment.raw["calendar"]["Earnings Average"] is None
    assert payment.first_seen_at.replace(tzinfo=UTC) == NOW


def test_a_dividend_no_longer_announced_is_deleted_only_on_a_good_calendar(monkeypatch, frozen_now):
    session_cm = _make_session_scope()
    _fetch(monkeypatch, session_cm, _raw_with(calendar=_calendar()))
    assert len(_corporate(session_cm)) == 2

    # A failed call (None) and yfinance's empty failure value change nothing.
    _fetch(monkeypatch, session_cm, _raw_with(calendar=None))
    _fetch(monkeypatch, session_cm, _raw_with(calendar={}))
    assert len(_corporate(session_cm)) == 2

    # A good calendar that dropped the dividend keys deletes both upcoming dates.
    _fetch(monkeypatch, session_cm, _raw_with(calendar={"Earnings Date": [date(2026, 10, 30)]}))
    assert _corporate(session_cm) == {}


def test_a_refreshed_corporate_event_keeps_first_seen(monkeypatch, frozen_now):
    session_cm = _make_session_scope()
    _fetch(monkeypatch, session_cm, _raw_with(calendar=_calendar()))
    marker = datetime(2026, 1, 1, tzinfo=UTC)
    with session_cm() as session:
        for row in session.scalars(select(CorporateEvent)):
            row.first_seen_at = marker

    _fetch(monkeypatch, session_cm, _raw_with(calendar=_calendar(**{"Earnings High": 2.0})))

    for row in _corporate(session_cm).values():
        assert row.first_seen_at.replace(tzinfo=UTC) == marker
        assert row.raw["calendar"]["Earnings High"] == 2.0


def test_calendar_value_helpers():
    assert yf_fund._calendar_dates([date(2026, 1, 2), "junk", None]) == [date(2026, 1, 2)]
    assert yf_fund._calendar_dates(pd.Timestamp("2026-03-04")) == [date(2026, 3, 4)]
    assert yf_fund._calendar_dates(object()) == []
    assert yf_fund._json_value((date(2026, 1, 2), True, "x", None)) == [
        "2026-01-02",
        True,
        "x",
        None,
    ]
    assert yf_fund._json_value(datetime(2026, 1, 2, 3, tzinfo=UTC)) == "2026-01-02T03:00:00+00:00"
    assert yf_fund._json_value(object()).startswith("<object")


class _FakeTicker:
    def __init__(self, symbol, info, calendar_error=False):
        self.info = info
        self.dividends = pd.Series(dtype=float)
        self.splits = pd.Series([2.0], index=pd.DatetimeIndex(["2022-01-03"]))
        self._calendar_error = calendar_error
        for attr in yf_fund._STATEMENT_ATTRS.values():
            setattr(self, attr, pd.DataFrame())

    def get_shares_full(self, start):
        return None

    def get_earnings_dates(self, limit):
        return pd.DataFrame()

    @property
    def calendar(self):
        if self._calendar_error:
            raise RuntimeError("calendar endpoint 404")
        return _calendar()


def _fake_yfinance(monkeypatch, **ticker_kwargs):
    module = types.ModuleType("yfinance")
    module.Ticker = lambda symbol: _FakeTicker(symbol, **ticker_kwargs)
    monkeypatch.setitem(sys.modules, "yfinance", module)
    monkeypatch.setattr(yf_fund, "throttle", lambda: None)


def test_download_fetches_splits_and_calendar(monkeypatch):
    _fake_yfinance(monkeypatch, info={"quoteType": "EQUITY"})
    raw = yf_fund._download_fundamentals("AAPL")
    assert list(raw.splits) == [2.0]
    assert raw.calendar["Ex-Dividend Date"] == date(2026, 11, 10)


def test_download_survives_a_failed_calendar_and_skips_it_for_etfs(monkeypatch, caplog):
    _fake_yfinance(monkeypatch, info={"quoteType": "EQUITY"}, calendar_error=True)
    assert yf_fund._download_fundamentals("AAPL").calendar is None
    assert "calendar" in caplog.text

    _fake_yfinance(monkeypatch, info={"quoteType": "ETF"})
    etf = yf_fund._download_fundamentals("XEQT.TO")
    assert etf.calendar is None and list(etf.splits) == [2.0]


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
