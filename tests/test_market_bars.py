from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market.bars import (
    LATEST_BAR_LOOKBACKS,
    bar_is_consistent,
    bar_ts_since,
    latest_bar_ts,
    upsert_daily_bars,
)
from data_lake.ingestion.market.yahoo_common import daily_ts


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine


def _bar(day: date, close: float) -> dict:
    return {
        "ts": daily_ts(day),
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 10.0,
    }


def _instrument(session: Session) -> int:
    instrument = Instrument(symbol="AAA", exchange="SMART", currency="USD")
    session.add(instrument)
    session.flush()
    return instrument.id


def test_inserts_then_updates_with_a_single_preload_query():
    engine = _engine()
    with Session(engine) as session:
        instrument_id = _instrument(session)
        assert (
            upsert_daily_bars(
                session,
                instrument_id,
                [_bar(date(2024, 1, 2), 10.0), _bar(date(2024, 1, 3), 11.0)],
                source="yahoo",
            )
            == 2
        )
        session.commit()

        selects: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _count(conn, cursor, statement, *args):
            if statement.lstrip().upper().startswith("SELECT"):
                selects.append(statement)

        written = upsert_daily_bars(
            session,
            instrument_id,
            [_bar(date(2024, 1, 3), 12.0), _bar(date(2024, 1, 4), 13.0)],
            source="yahoo",
        )
        session.commit()
        event.remove(engine, "before_cursor_execute", _count)

        assert written == 2
        assert len(selects) == 1  # one preload, no per-bar lookups
        bars = session.scalars(select(PriceBar).order_by(PriceBar.ts)).all()
        assert [(b.ts.date(), b.close) for b in bars] == [
            (date(2024, 1, 2), 10.0),
            (date(2024, 1, 3), 12.0),
            (date(2024, 1, 4), 13.0),
        ]
        assert all(b.what_to_show == "ADJUSTED_LAST" and b.bar_size == "1 day" for b in bars)


def test_keeps_sources_and_series_apart():
    engine = _engine()
    with Session(engine) as session:
        instrument_id = _instrument(session)
        upsert_daily_bars(session, instrument_id, [_bar(date(2024, 1, 2), 10.0)], source="yahoo")
        upsert_daily_bars(session, instrument_id, [_bar(date(2024, 1, 2), 20.0)], source="tiingo")
        upsert_daily_bars(
            session,
            instrument_id,
            [_bar(date(2024, 1, 2), 30.0)],
            source="yahoo",
            what_to_show="TRADES",
        )
        session.commit()
        closes = sorted(b.close for b in session.scalars(select(PriceBar)))
        assert closes == [10.0, 20.0, 30.0]


def test_repeated_ts_in_one_batch_updates_rather_than_duplicates():
    engine = _engine()
    with Session(engine) as session:
        instrument_id = _instrument(session)
        upsert_daily_bars(
            session,
            instrument_id,
            [_bar(date(2024, 1, 2), 10.0), _bar(date(2024, 1, 2), 11.0)],
            source="yahoo",
        )
        session.commit()
        bars = session.scalars(select(PriceBar)).all()
        assert [b.close for b in bars] == [11.0]


def test_empty_values_write_nothing():
    engine = _engine()
    with Session(engine) as session:
        assert upsert_daily_bars(session, _instrument(session), iter([]), source="yahoo") == 0


#: CPAY 2026-10-08 as a mid-session Yahoo poll stored it: close above high, 3,742 shares.
_CPAY_PARTIAL = {
    "ts": daily_ts(date(2026, 10, 8)),
    "open": 401.97,
    "high": 402.86,
    "low": 401.78,
    "close": 402.955,
    "volume": 3742.0,
}


def test_skips_a_bar_whose_close_is_outside_its_range(caplog):
    engine = _engine()
    with Session(engine) as session:
        instrument_id = _instrument(session)
        good = _bar(date(2026, 10, 7), 402.43)
        written = upsert_daily_bars(session, instrument_id, [good, _CPAY_PARTIAL], source="yahoo")
        session.commit()

        assert written == 1
        bars = session.scalars(select(PriceBar)).all()
        assert [b.ts.date() for b in bars] == [date(2026, 10, 7)]
    assert "skipped inconsistent yahoo bar" in caplog.text
    assert "2026-10-08" in caplog.text


def test_an_inconsistent_refetch_leaves_the_stored_bar_alone():
    engine = _engine()
    with Session(engine) as session:
        instrument_id = _instrument(session)
        stored = {**_CPAY_PARTIAL, "high": 403.76, "low": 393.37, "close": 402.08}
        upsert_daily_bars(session, instrument_id, [stored], source="yahoo")
        session.commit()

        assert upsert_daily_bars(session, instrument_id, [_CPAY_PARTIAL], source="yahoo") == 0
        session.commit()
        bar = session.scalars(select(PriceBar)).one()
        assert (bar.high, bar.low, bar.close) == (403.76, 393.37, 402.08)


def _ohlc(open_: float, high: float, low: float, close: float) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close}


def test_bar_is_consistent_accepts_prices_on_the_range_edges():
    assert bar_is_consistent(_ohlc(10.0, 12.0, 10.0, 12.0))
    assert bar_is_consistent(_ohlc(5.0, 5.0, 5.0, 5.0))


def test_bar_is_consistent_absorbs_adjustment_rounding_only():
    assert bar_is_consistent(_ohlc(100.0, 101.0, 99.0, 101.0 * (1 + 1e-12)))
    # A 10-09 as stored minutes after the close: open 2 cents under the low Yahoo had then.
    assert not bar_is_consistent(_ohlc(168.57, 172.19, 168.59, 170.54))


def test_bar_is_consistent_rejects_each_way_out_of_range():
    assert not bar_is_consistent(_ohlc(13.0, 12.0, 10.0, 11.0))  # open above high
    assert not bar_is_consistent(_ohlc(9.0, 12.0, 10.0, 11.0))  # open below low
    assert not bar_is_consistent(_ohlc(11.0, 12.0, 10.0, 9.0))  # close below low
    assert not bar_is_consistent(_ohlc(11.0, 10.0, 12.0, 11.0))  # low above high
    assert not bar_is_consistent(_ohlc(float("nan"), 12.0, 10.0, 11.0))


_NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def _newest_after_seeding(*ages_days: int) -> tuple[datetime | None, list[str]]:
    """Seed one yahoo bar per age, run ``latest_bar_ts``, return its answer and its SELECTs."""
    engine = _engine()
    with Session(engine) as session:
        instrument_id = _instrument(session)
        days = [(_NOW - timedelta(days=age)).date() for age in ages_days]
        upsert_daily_bars(session, instrument_id, [_bar(day, 10.0) for day in days], source="yahoo")
        session.commit()

        selects: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _record(conn, cursor, statement, *args):
            selects.append(statement)

        newest = latest_bar_ts(
            session,
            select(func.max(PriceBar.ts)).where(PriceBar.instrument_id == instrument_id),
            now=_NOW,
        )
    return newest, selects


def test_latest_bar_ts_answers_from_the_nearest_window_in_one_query():
    newest, selects = _newest_after_seeding(3, 20, 900)
    assert newest is not None
    assert newest.date() == (_NOW - timedelta(days=3)).date()
    assert len(selects) == 1
    cutoff = (_NOW - LATEST_BAR_LOOKBACKS[0]).strftime("%Y-%m-%d %H:%M:%S")
    assert cutoff in selects[0]  # written into the SQL, not bound: see bar_ts_since


def test_latest_bar_ts_widens_the_window_before_it_scans_everything():
    newest, selects = _newest_after_seeding(200)
    assert newest is not None
    assert newest.date() == (_NOW - timedelta(days=200)).date()
    assert len(selects) == 2


def test_latest_bar_ts_falls_back_to_the_unbounded_query():
    newest, selects = _newest_after_seeding(900)
    assert newest is not None
    assert newest.date() == (_NOW - timedelta(days=900)).date()
    assert len(selects) == len(LATEST_BAR_LOOKBACKS) + 1
    assert ">=" not in selects[-1]


def test_latest_bar_ts_is_none_for_a_series_with_no_bars():
    newest, selects = _newest_after_seeding()
    assert newest is None
    assert len(selects) == len(LATEST_BAR_LOOKBACKS) + 1


def test_bar_ts_since_is_a_constant_postgres_can_exclude_chunks_by():
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)
    statement = select(func.max(PriceBar.ts)).where(
        PriceBar.instrument_id == 7, bar_ts_since(cutoff)
    )
    sql = str(
        statement.compile(
            dialect=postgresql.psycopg.dialect(), compile_kwargs={"render_postcompile": True}
        )
    )
    assert "price_bars.ts >= '2026-09-01 00:00:00+00:00'" in sql
    assert "%(instrument_id_1)s" in sql  # the other filters stay bound parameters
