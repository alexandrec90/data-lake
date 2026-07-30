from datetime import UTC, date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.db.models import Base, NewsArticle
from data_lake.ingestion.news import finnhub_backfill
from data_lake.ingestion.news.finnhub_backfill import (
    SPLIT_THRESHOLD,
    backfill_symbols,
    earliest_published_by_symbol,
    run_backfill,
)

TODAY = date(2026, 7, 17)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


class RecordingFetch:
    """Fake FetchWindow capturing every (symbol, from, to) call."""

    def __init__(self, count_for=None):
        self.calls: list[tuple[str, date, date]] = []
        self._count_for = count_for or (lambda symbol, a, b: 5)

    def __call__(self, symbol: str, w_from: date, w_to: date) -> int:
        self.calls.append((symbol, w_from, w_to))
        return self._count_for(symbol, w_from, w_to)


class TestBackfillSymbols:
    def test_walks_windows_backwards_from_earliest_to_floor(self):
        fetch = RecordingFetch()
        total = backfill_symbols(
            ["AAPL"],
            {"AAPL": date(2026, 7, 8)},
            fetch,
            today=TODAY,
            backfill_days=90,
            chunk_days=30,
            sleep=lambda s: None,
        )
        floor = date(2026, 4, 18)
        assert fetch.calls == [
            ("AAPL", date(2026, 6, 8), date(2026, 7, 8)),
            ("AAPL", date(2026, 5, 9), date(2026, 6, 8)),
            ("AAPL", date(2026, 4, 18), date(2026, 5, 9)),  # last window clamps to the floor
        ]
        assert fetch.calls[-1][1] == floor
        assert total == 15

    def test_symbol_with_no_stored_articles_starts_at_today(self):
        fetch = RecordingFetch()
        backfill_symbols(
            ["NEWCO"], {}, fetch, today=TODAY, backfill_days=30, chunk_days=30, sleep=lambda s: None
        )
        assert fetch.calls == [("NEWCO", TODAY.replace(month=6), TODAY)]

    def test_history_already_at_floor_costs_zero_requests(self):
        fetch = RecordingFetch()
        total = backfill_symbols(
            ["AAPL"],
            {"AAPL": date(2025, 7, 1)},
            fetch,
            today=TODAY,
            backfill_days=365,
            sleep=lambda s: None,
        )
        assert fetch.calls == []
        assert total == 0

    def test_capped_response_splits_window_in_half(self):
        # The first (widest) window looks capped -> its halves are fetched; halves are small
        # enough to come back uncapped.
        def count_for(symbol, a, b):
            return SPLIT_THRESHOLD if (b - a).days >= 30 else 10

        fetch = RecordingFetch(count_for)
        total = backfill_symbols(
            ["AAPL"],
            {"AAPL": date(2026, 7, 8)},
            fetch,
            today=TODAY,
            backfill_days=40,
            chunk_days=30,
            sleep=lambda s: None,
        )
        capped = ("AAPL", date(2026, 6, 8), date(2026, 7, 8))
        halves = {
            ("AAPL", date(2026, 6, 8), date(2026, 6, 23)),
            ("AAPL", date(2026, 6, 23), date(2026, 7, 8)),
        }
        assert fetch.calls[0] == capped
        assert halves <= set(fetch.calls)
        # the capped fetch's own count is not added — its halves cover the window
        assert total == 10 * (len(fetch.calls) - 1)

    def test_one_day_window_still_capped_is_accepted_not_looped(self):
        fetch = RecordingFetch(lambda symbol, a, b: SPLIT_THRESHOLD)
        total = backfill_symbols(
            ["AAPL"],
            {"AAPL": date(2026, 7, 16)},
            fetch,
            today=TODAY,
            backfill_days=2,
            chunk_days=1,
            sleep=lambda s: None,
        )
        assert fetch.calls == [("AAPL", date(2026, 7, 15), date(2026, 7, 16))]
        assert total == SPLIT_THRESHOLD

    def test_request_budget_stops_the_run_mid_walk(self):
        fetch = RecordingFetch()
        backfill_symbols(
            ["AAPL", "MSFT"],
            {},
            fetch,
            today=TODAY,
            backfill_days=365,
            chunk_days=30,
            max_requests=3,
            sleep=lambda s: None,
        )
        assert len(fetch.calls) == 3
        assert {symbol for symbol, *_ in fetch.calls} == {"AAPL"}  # never reached MSFT

    def test_failing_symbol_is_skipped_not_fatal(self):
        def count_for(symbol, a, b):
            if symbol == "BAD":
                raise RuntimeError("provider says no")
            return 1

        fetch = RecordingFetch(count_for)
        total = backfill_symbols(
            ["BAD", "AAPL"],
            {},
            fetch,
            today=TODAY,
            backfill_days=30,
            chunk_days=30,
            sleep=lambda s: None,
        )
        symbols_called = [symbol for symbol, *_ in fetch.calls]
        assert "AAPL" in symbols_called
        assert total == 1

    def test_calls_are_spaced_with_the_configured_delay(self):
        sleeps: list[float] = []
        fetch = RecordingFetch()
        backfill_symbols(
            ["AAPL"],
            {},
            fetch,
            today=TODAY,
            backfill_days=90,
            chunk_days=30,
            request_spacing_seconds=1.5,
            sleep=sleeps.append,
        )
        # first call unspaced, every later call preceded by one sleep
        assert sleeps == [1.5] * (len(fetch.calls) - 1)


def test_earliest_published_by_symbol_takes_min_per_tag():
    session = _session()

    def article(external_id, published, symbols):
        return NewsArticle(
            source="finnhub",
            external_id=external_id,
            published_at=published,
            title="t",
            symbols=symbols,
            fetched_at=datetime.now(UTC),
        )

    session.add_all(
        [
            article("1", datetime(2026, 7, 10, tzinfo=UTC), ["AAPL"]),
            article("2", datetime(2026, 7, 8, tzinfo=UTC), ["AAPL", "MSFT"]),
            article("3", datetime(2026, 7, 12, tzinfo=UTC), ["MSFT"]),
            article("4", datetime(2026, 7, 1, tzinfo=UTC), None),  # untagged -> ignored
        ]
    )
    other = NewsArticle(
        source="newsapi",
        external_id="x",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        title="t",
        symbols=["AAPL"],
        fetched_at=datetime.now(UTC),
    )
    session.add(other)
    session.commit()

    earliest = earliest_published_by_symbol(session)
    assert earliest == {"AAPL": date(2026, 7, 8), "MSFT": date(2026, 7, 8)}


def test_run_backfill_wires_connector_and_cursors(monkeypatch):
    from contextlib import contextmanager

    session = _session()
    session.add(
        NewsArticle(
            source="finnhub",
            external_id="1",
            published_at=datetime(2026, 7, 8, tzinfo=UTC),
            title="t",
            symbols=["AAPL"],
            fetched_at=datetime.now(UTC),
        )
    )
    session.commit()

    @contextmanager
    def fake_get_session():
        yield session

    windows: list[tuple[str, str, str]] = []

    def fake_fetch(self, symbol="", date_from="", date_to="", **kwargs):
        windows.append((symbol, date_from, date_to))
        return 2

    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

    monkeypatch.setattr(FinnhubNewsConnector, "fetch", fake_fetch)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 17, tzinfo=tz)

    monkeypatch.setattr(finnhub_backfill, "datetime", FixedDatetime)

    total = run_backfill(
        ["AAPL"],
        backfill_days=60,
        chunk_days=30,
        sleep=lambda s: None,
        session_factory=fake_get_session,
    )

    # cursor came from the stored article (2026-07-08), dates passed as ISO strings
    assert windows == [
        ("AAPL", "2026-06-08", "2026-07-08"),
        ("AAPL", "2026-05-18", "2026-06-08"),
    ]
    assert total == 4
