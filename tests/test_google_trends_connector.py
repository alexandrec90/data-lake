from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from data_lake.db.models import Base, TrendPoint
from data_lake.ingestion.social import google_trends as gt


class FakeTrendReq:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame
        self.payloads: list[dict] = []

    def build_payload(self, kw_list, timeframe, geo):
        self.payloads.append({"kw_list": kw_list, "timeframe": timeframe, "geo": geo})

    def interest_over_time(self) -> pd.DataFrame:
        return self._frame


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(gt, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(gt, "_last_request_monotonic", None)


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


def _frame(keyword: str, values: list[float], is_partial: list[bool], dates: list[str]):
    return pd.DataFrame(
        {keyword: values, "isPartial": is_partial},
        index=pd.DatetimeIndex(dates),
    )


def test_trends_fetch_upserts_points_and_skips_partial(monkeypatch):
    session_cm = _make_session_scope()
    frame = _frame(
        "AAPL",
        values=[40.0, 55.0, 60.0],
        is_partial=[False, False, True],  # last bucket is provisional
        dates=["2024-01-01", "2024-01-02", "2024-01-03"],
    )
    client = FakeTrendReq(frame)
    monkeypatch.setattr(gt, "_trends_client", lambda: client)

    count = gt.GoogleTrendsConnector(session_factory=session_cm).fetch(keywords=["AAPL"], geo="CA")

    assert count == 2  # partial row skipped
    assert client.payloads[0]["timeframe"] == gt.DEFAULT_TIMEFRAME
    assert client.payloads[0]["geo"] == "CA"
    with session_cm() as session:
        points = session.scalars(select(TrendPoint).order_by(TrendPoint.ts)).all()
        assert [p.interest for p in points] == [40.0, 55.0]
        assert points[0].keyword == "AAPL"
        assert points[0].geo == "CA"
        # SQLite drops tzinfo on readback (Postgres keeps it); compare the naive instant.
        assert points[0].ts.replace(tzinfo=UTC) == datetime(2024, 1, 1, tzinfo=UTC)


def test_trends_fetch_is_idempotent(monkeypatch):
    session_cm = _make_session_scope()

    def run(value):
        frame = _frame("AAPL", [value], [False], ["2024-01-01"])
        monkeypatch.setattr(gt, "_trends_client", lambda: FakeTrendReq(frame))
        return gt.GoogleTrendsConnector(session_factory=session_cm).fetch(
            keywords=["AAPL"], geo="CA"
        )

    assert run(40.0) == 1
    assert run(70.0) == 1  # same (keyword, geo, ts) -> update

    with session_cm() as session:
        points = session.scalars(select(TrendPoint)).all()
        assert len(points) == 1
        assert points[0].interest == 70.0


def test_trends_empty_frame_returns_zero(monkeypatch):
    session_cm = _make_session_scope()
    monkeypatch.setattr(gt, "_trends_client", lambda: FakeTrendReq(pd.DataFrame()))

    assert gt.GoogleTrendsConnector(session_factory=session_cm).fetch(keywords=["AAPL"]) == 0


def test_trends_requires_keyword():
    with pytest.raises(ValueError, match="keyword"):
        gt.GoogleTrendsConnector().fetch(keywords=[])


def test_trends_skips_network_when_series_is_fresh(monkeypatch):
    session_cm = _make_session_scope()
    with session_cm() as session:
        session.add(
            TrendPoint(
                keyword="AAPL stock",
                geo="CA",
                ts=datetime.now(UTC) - timedelta(days=2),
                interest=50.0,
            )
        )

    def no_client():
        raise AssertionError("fresh series must not hit the network")

    monkeypatch.setattr(gt, "_trends_client", no_client)

    count = gt.GoogleTrendsConnector(session_factory=session_cm).fetch(
        keywords=["AAPL stock"], geo="CA", skip_if_newer_than_days=14
    )
    assert count == 0


def test_trends_fetches_when_series_is_stale(monkeypatch):
    session_cm = _make_session_scope()
    with session_cm() as session:
        session.add(
            TrendPoint(
                keyword="AAPL stock",
                geo="CA",
                ts=datetime.now(UTC) - timedelta(days=30),
                interest=50.0,
            )
        )
    frame = _frame("AAPL stock", [60.0], [False], ["2024-01-01"])
    client = FakeTrendReq(frame)
    monkeypatch.setattr(gt, "_trends_client", lambda: client)

    count = gt.GoogleTrendsConnector(session_factory=session_cm).fetch(
        keywords=["AAPL stock"], geo="CA", skip_if_newer_than_days=14
    )
    assert count == 1
    assert client.payloads  # network was used


def test_trends_freshness_filter_drops_only_fresh_keywords(monkeypatch):
    """Mixed batch: the fresh keyword is filtered out of the payload, the stale one fetched."""
    session_cm = _make_session_scope()
    with session_cm() as session:
        session.add(
            TrendPoint(
                keyword="fresh", geo="CA", ts=datetime.now(UTC) - timedelta(days=1), interest=10.0
            )
        )
    frame = _frame("stale", [30.0], [False], ["2024-01-01"])
    client = FakeTrendReq(frame)
    monkeypatch.setattr(gt, "_trends_client", lambda: client)

    gt.GoogleTrendsConnector(session_factory=session_cm).fetch(
        keywords=["fresh", "stale"], geo="CA", skip_if_newer_than_days=14
    )
    assert client.payloads[0]["kw_list"] == ["stale"]


def test_read_mapping_file_parses_pairs_and_skips_comments(tmp_path):
    mapping = tmp_path / "m.txt"
    mapping.write_text(
        "# comment\n\nAAPL,Apple\nry , RBC \nTD,TD Bank\n",
        encoding="utf-8",
    )
    assert gt.read_mapping_file(str(mapping)) == [
        ("AAPL", "Apple"),
        ("RY", "RBC"),
        ("TD", "TD Bank"),
    ]


def test_read_mapping_file_allows_multiple_keywords_per_ticker(tmp_path):
    """A ticker may map to several search terms (e.g. brand vs 'TICKER stock' variants);
    each becomes its own series and joins back to the same symbol at feature time."""
    mapping = tmp_path / "m.txt"
    mapping.write_text("TSLA,TSLA stock\nTSLA,Tesla\n", encoding="utf-8")
    assert gt.read_mapping_file(str(mapping)) == [
        ("TSLA", "TSLA stock"),
        ("TSLA", "Tesla"),
    ]


def test_read_mapping_file_tolerates_bom(tmp_path):
    mapping = tmp_path / "m.txt"
    mapping.write_bytes(b"\xef\xbb\xbfAAPL,Apple\n")  # Notepad-style UTF-8 BOM
    assert gt.read_mapping_file(str(mapping)) == [("AAPL", "Apple")]


def test_read_mapping_file_rejects_malformed_line(tmp_path):
    mapping = tmp_path / "m.txt"
    mapping.write_text("AAPL Apple\n", encoding="utf-8")  # missing comma
    with pytest.raises(ValueError, match="TICKER,search term"):
        gt.read_mapping_file(str(mapping))


def test_trends_caps_at_five_keywords(monkeypatch):
    session_cm = _make_session_scope()
    six = ["A", "B", "C", "D", "E", "F"]
    frame = pd.DataFrame(
        {kw: [10.0] for kw in six[:5]} | {"isPartial": [False]},
        index=pd.DatetimeIndex(["2024-01-01"]),
    )
    client = FakeTrendReq(frame)
    monkeypatch.setattr(gt, "_trends_client", lambda: client)

    count = gt.GoogleTrendsConnector(session_factory=session_cm).fetch(keywords=six)

    assert client.payloads[0]["kw_list"] == ["A", "B", "C", "D", "E"]
    assert count == 5
