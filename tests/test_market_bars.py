from datetime import date

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from data_lake.db.models import Base, Instrument, PriceBar
from data_lake.ingestion.market.bars import upsert_daily_bars
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
