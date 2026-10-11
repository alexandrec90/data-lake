from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest
from sqlalchemy import func, select

from _db import make_session_scope
from _lake_env import SETTINGS
from data_lake.db.models import Instrument, IpoEvent, PriceBar
from data_lake.ingestion.market import tiingo, yahoo, yahoo_common
from data_lake.ingestion.market.ipo_pricing import (
    FIRST_BAR_EARLY,
    FIRST_BAR_LATE,
    MIN_BARS,
    SPAC_MEDIAN_MOVE,
    SPAC_MIN_BARS,
    SPAC_WINDOW_BARS,
    IpoPricingOptions,
    exclusion,
    provider_symbol,
    resolve_ipo_prices,
    starts_at_listing,
    trades_like_a_spac,
)
from data_lake.ingestion.market.tiingo import TiingoListing, TiingoUsage

SINCE = date(2013, 1, 1)
TODAY = date(2024, 6, 28)
OPTIONS = IpoPricingOptions(today=TODAY)
IPO = date(2020, 10, 14)


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(yahoo_common, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(yahoo_common, "_last_request_monotonic", None)


@pytest.fixture
def scope():
    return make_session_scope()


def _weekdays(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    return [day for day in days if day.weekday() < 5]


def _wiggle(days: list[date], step: float = 0.04) -> list[float]:
    """Closes that move ``step`` a day, up and down: a company's trading, not a trust's."""
    return [20.0 * (1 + step) ** (n % 2) for n in range(len(days))]


def _event(scope, symbol, ipo=IPO, *, name="Codiak BioSciences, Inc.", price=15.0, **extra):
    with scope() as session:
        row = IpoEvent(
            source=extra.pop("source", "finnhub"),
            external_id=f"{symbol}-{ipo}-{name}",
            company_name=name,
            symbol=symbol,
            stage=extra.pop("stage", "priced"),
            expected_date=ipo,
            price_low=price,
            price_high=price,
            first_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
            fetched_at=datetime(2026, 10, 1, tzinfo=UTC),
            **extra,
        )
        session.add(row)
        session.flush()
        return row.id


def _get(scope, event_id) -> IpoEvent:
    with scope() as session:
        row = session.get(IpoEvent, event_id)
        assert row is not None
        return row


def _instrument(scope, instrument_id) -> Instrument:
    with scope() as session:
        instrument = session.get(Instrument, instrument_id)
        assert instrument is not None
        return instrument


def _frame(days: list[date], closes: list[float] | None = None) -> pd.DataFrame:
    closes = closes if closes is not None else _wiggle(days)
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1.0},
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in days], tz="America/New_York"),
    )


def _yahoo(monkeypatch, frames: dict[str, object]) -> list[tuple]:
    """Stub the Yahoo download: a DataFrame is filtered to the asked window, an error raised."""
    calls: list[tuple] = []

    def fake(symbol, start, end, auto_adjust):
        calls.append((symbol, start, end))
        result = frames.get(symbol, pd.DataFrame())
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, pd.DataFrame)
        if result.empty:
            return result
        keep = [start <= ts.date() < end for ts in result.index]
        return result[keep]

    monkeypatch.setattr(yahoo, "_download_history", fake)
    return calls


def _tiingo(monkeypatch, scope, days_by_ticker: dict[str, object], usage=None):
    """Stub Tiingo's prices endpoint; an int is an HTTP status, a day list a series."""
    monkeypatch.setattr(SETTINGS, "tiingo_api_key", "k")
    calls: list[str] = []

    def fake(ticker, params, token):
        calls.append(ticker)
        result = days_by_ticker.get(ticker, 404)
        request = httpx.Request("GET", "https://api.tiingo.com/x")
        if isinstance(result, int):
            return httpx.Response(result, request=request)
        first = date.fromisoformat(params["startDate"])
        last = date.fromisoformat(params.get("endDate", "2999-01-01"))
        days = [day for day in result if first <= day <= last]
        return httpx.Response(
            200,
            json=[
                {"date": f"{d.isoformat()}T00:00:00.000Z", "adjClose": close}
                for d, close in zip(days, _wiggle(days), strict=True)
            ],
            request=request,
        )

    monkeypatch.setattr(tiingo, "_get_prices", fake)
    return tiingo.TiingoConnector(session_factory=scope, usage=usage), calls


def _listing(ticker, start=IPO, end=None, exchange="NASDAQ", asset_type="Stock"):
    return TiingoListing(ticker, exchange, asset_type, "USD", start, end)


def _store_bars(scope, symbol, days, *, source="yahoo", exchange="SMART", closes=None) -> int:
    closes = closes if closes is not None else _wiggle(days)
    with scope() as session:
        instrument = Instrument(symbol=symbol, exchange=exchange, currency="USD")
        session.add(instrument)
        session.flush()
        for day, close in zip(days, closes, strict=True):
            session.add(
                PriceBar(
                    instrument_id=instrument.id,
                    ts=yahoo_common.daily_ts(day),
                    bar_size="1 day",
                    source=source,
                    what_to_show="ADJUSTED_LAST",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1.0,
                )
            )
        return instrument.id


def _bar_count(scope, instrument_id) -> int:
    with scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(PriceBar)
            .where(PriceBar.instrument_id == instrument_id)
        )


def _run(scope, **kwargs):
    kwargs.setdefault("options", OPTIONS)
    return resolve_ipo_prices(since=SINCE, session_factory=scope, **kwargs)


# --- exclusion -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("symbol", "name", "reason"),
    [
        ("CONXU", "CONX Corp.", "unit"),
        ("NBA'U", "New Beginnings Acquisition Corp.", "unit"),
        ("IPOEU", "SoFi Technologies, Inc.", "unit"),  # Finnhub's post-merger name
        ("PSTH.U", "Pershing Square Tontine", "unit"),
        ("ABCDW", "Abcd Inc.", "warrant"),
        ("PL.WS", "Planet Labs", "warrant"),
        ("ABCDR", "Abcd Inc.", "right"),
        ("ABR-PD", "Arbor Realty", "preferred"),
        ("XYZ", "XYZ 8.00% Series A Cumulative Preferred Stock", "preferred"),
        ("XYZ", "XYZ Corp 6.5% Notes due 2029", "preferred"),
        ("HLXA", "Helix Acquisition Corp", "spac"),
        ("OMEG", "Omega Alpha SPAC", "spac"),
        ("DYNS", "Dynamics Special Purpose Corp.", "spac"),
        ("FGMC", "FG Merger II Corp.", "spac"),
        ("GLDM", "WORLD GOLD TRUST", "fund"),
        ("BITB", "Bitwise Bitcoin ETF", "fund"),
        ("TONS", "WISDOMTREE COAL FUND", "fund"),
        ("FXSG", "INVESCO CURRENCYSHARES SINGAPORE DOLLAR TRUST", "fund"),
        (None, "Meey Global Corp", "no-symbol"),
        ("", "Meey Global Corp", "no-symbol"),
    ],
)
def test_exclusion_sets_aside_what_is_not_common_stock(symbol, name, reason):
    assert exclusion(symbol, name) == reason


@pytest.mark.parametrize(
    ("symbol", "name"),
    [
        ("ROOT", "Root, Inc."),
        ("LU", "Lufax Holding Ltd"),  # an ADR
        ("BRK.B", "Berkshire Hathaway"),  # a share class
        ("FWONA", "Liberty Formula One"),  # Nasdaq's fifth letter A is a class
        ("DOC", "PHYSICIANS REALTY TRUST"),  # a REIT is common stock
        ("FMSA", "BISON MERGER SUB I, LLC"),  # Fairmount Santrol, a real listing
        ("ROKU", "Roku, Inc."),  # a four-letter ticker ending in U
    ],
)
def test_exclusion_keeps_common_stock_and_adrs(symbol, name):
    assert exclusion(symbol, name) is None


def test_provider_symbol_uses_a_dash_for_share_classes():
    assert provider_symbol(" brk.b ") == "BRK-B"


def test_starts_at_listing_bounds():
    assert starts_at_listing(IPO, MIN_BARS, IPO)
    assert starts_at_listing(IPO - FIRST_BAR_EARLY, MIN_BARS, IPO)
    assert starts_at_listing(IPO + FIRST_BAR_LATE, MIN_BARS, IPO)
    assert not starts_at_listing(IPO - FIRST_BAR_EARLY - timedelta(days=1), MIN_BARS, IPO)
    assert not starts_at_listing(IPO + FIRST_BAR_LATE + timedelta(days=1), MIN_BARS, IPO)
    assert not starts_at_listing(IPO, MIN_BARS - 1, IPO)


def test_trades_like_a_spac():
    assert trades_like_a_spac([10.0, 10.01, 10.0, 10.02] * 10) is True
    assert trades_like_a_spac([10.0, 11.0] * 20) is False
    assert trades_like_a_spac([10.0] * (SPAC_MIN_BARS - 1)) is None
    # A merger-rumour spike does not hide the weeks pinned to the trust around it.
    assert trades_like_a_spac([10.0] * 30 + [14.0, 10.0] + [10.0] * 30) is True


def _alternating(move: float) -> list[float]:
    return [10.0 * (1 + move) ** (n % 2) for n in range(SPAC_WINDOW_BARS)]


@pytest.mark.parametrize(
    ("median_move", "verdict"),
    [
        (0.0087, True),  # RACD, an unnamed shares-only SPAC: the quietest-moving company...
        (0.0091, False),  # ...EBC and CDNA, sat just above it
        (0.0300, False),
    ],
)
def test_trades_like_a_spac_at_the_calibrated_boundary(median_move, verdict):
    assert SPAC_MEDIAN_MOVE == 0.009
    assert trades_like_a_spac(_alternating(median_move)) is verdict


# --- resolving ------------------------------------------------------------------------------


def test_a_live_listing_is_priced_from_yahoo(scope, monkeypatch):
    event = _event(scope, "CDAK")
    days = _weekdays(IPO, TODAY)
    calls = _yahoo(monkeypatch, {"CDAK": _frame(days)})

    report = _run(scope)

    assert report.resolved_yahoo == 1
    row = _get(scope, event)
    assert row.resolution == "yahoo"
    assert row.resolved_at is not None
    assert _bar_count(scope, row.instrument_id) == len(days)
    assert _instrument(scope, row.instrument_id).listed_on == IPO
    [(symbol, start, _)] = calls
    assert (symbol, start) == ("CDAK", IPO - timedelta(days=30))  # asked from before the IPO


def test_listed_on_is_the_earlier_of_ipo_date_and_first_bar(scope, monkeypatch):
    """Finnhub's date is sometimes the pricing day, the evening before the first trade."""
    event = _event(scope, "CDAK", ipo=date(2020, 10, 15))
    _yahoo(monkeypatch, {"CDAK": _frame(_weekdays(date(2020, 10, 14), TODAY))})

    _run(scope)

    assert _instrument(scope, _get(scope, event).instrument_id).listed_on == date(2020, 10, 14)


def test_a_ticker_reused_by_a_later_company_is_not_priced_with_its_history(scope, monkeypatch):
    """ADPT was Adeptus from 2014 and Adaptive from 2019; Yahoo serves only the latter."""
    event = _event(scope, "ADPT", ipo=date(2014, 6, 25), name="ADEPTUS HEALTH INC.")
    _yahoo(monkeypatch, {"ADPT": _frame(_weekdays(date(2019, 6, 27), TODAY))})

    report = _run(scope)

    assert report.unpriced == 1
    row = _get(scope, event)
    assert (row.resolution, row.instrument_id) == ("unpriced", None)
    with scope() as session:
        assert session.scalar(select(func.count()).select_from(Instrument)) == 0


def test_a_series_with_history_before_the_ipo_is_refused(scope, monkeypatch):
    """An earlier company under the ticker, or an OTC security "listing" by uplisting."""
    event = _event(scope, "ABCM")
    _yahoo(monkeypatch, {"ABCM": _frame(_weekdays(IPO - timedelta(days=20), TODAY))})

    assert _run(scope).unpriced == 1
    assert _get(scope, event).instrument_id is None


def test_a_dead_listing_is_priced_from_its_q_ticker_on_tiingo(scope, monkeypatch):
    """Tiingo files Codiak only as CDAKQ, from the IPO: and Yahoo is not asked at all."""
    event = _event(scope, "CDAK")
    yahoo_calls = _yahoo(monkeypatch, {})
    end = date(2023, 9, 18)
    listings = {
        "CDAKQ": [
            _listing("CDAKQ", date(2023, 4, 4), date(2023, 4, 5), "PINK"),
            _listing("CDAKQ", IPO, end, "PINK"),
        ]
    }
    connector, tiingo_calls = _tiingo(monkeypatch, scope, {"CDAKQ": _weekdays(IPO, end)})

    report = _run(scope, tiingo=connector, tiingo_listings=listings)

    assert report.resolved_tiingo == 1
    assert yahoo_calls == []
    assert tiingo_calls == ["CDAKQ"]
    row = _get(scope, event)
    assert row.resolution == "tiingo:CDAKQ"
    instrument = _instrument(scope, row.instrument_id)
    assert (instrument.symbol, instrument.exchange, instrument.listed_on) == ("CDAKQ", "PINK", IPO)


def test_a_live_listing_yahoo_lacks_falls_back_to_tiingo(scope, monkeypatch):
    event = _event(scope, "CDAK")
    yahoo_calls = _yahoo(monkeypatch, {})
    connector, _ = _tiingo(monkeypatch, scope, {"CDAK": _weekdays(IPO, TODAY)})

    report = _run(scope, tiingo=connector, tiingo_listings={"CDAK": [_listing("CDAK")]})

    assert report.resolved_tiingo == 1
    assert len(yahoo_calls) == 1
    assert _get(scope, event).resolution == "tiingo:CDAK"


def test_a_tiingo_series_that_starts_elsewhere_is_refused(scope, monkeypatch):
    """The listing's dates say IPO, but the API serves another security under the ticker."""
    event = _event(scope, "CDAK")
    end = date(2023, 9, 18)
    listings = {"CDAK": [_listing("CDAK", IPO, end)]}
    connector, _ = _tiingo(monkeypatch, scope, {"CDAK": _weekdays(date(2022, 1, 3), end)})
    _yahoo(monkeypatch, {})

    assert _run(scope, tiingo=connector, tiingo_listings=listings).unpriced == 1
    assert _get(scope, event).instrument_id is None


def test_a_tiingo_listing_that_started_elsewhere_is_not_fetched(scope, monkeypatch):
    _event(scope, "ADPT", ipo=date(2014, 6, 25))
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {})
    listings = {"ADPT": [_listing("ADPT", date(2019, 6, 27))]}

    assert _run(scope, tiingo=connector, tiingo_listings=listings).unpriced == 1
    assert calls == []


def test_tiingo_bars_of_an_earlier_company_are_not_mixed_in(scope, monkeypatch):
    event = _event(scope, "MSP", ipo=date(2020, 10, 21))
    end = date(2022, 6, 23)
    taken = _store_bars(
        scope,
        "MSP",
        _weekdays(date(2015, 1, 5), date(2015, 3, 2)),
        source="tiingo",
        exchange="NYSE",
    )
    before = _bar_count(scope, taken)
    _yahoo(monkeypatch, {})
    connector, calls = _tiingo(monkeypatch, scope, {"MSP": _weekdays(date(2020, 10, 21), end)})
    listings = {"MSP": [_listing("MSP", date(2020, 10, 21), end, "NYSE")]}

    assert _run(scope, tiingo=connector, tiingo_listings=listings).unpriced == 1
    assert calls == []
    assert _bar_count(scope, taken) == before
    assert _get(scope, event).instrument_id is None


def test_yahoo_bars_of_an_earlier_company_are_not_mixed_in(scope, monkeypatch):
    event = _event(scope, "ADPT", ipo=date(2019, 6, 27))
    taken = _store_bars(scope, "ADPT", _weekdays(date(2014, 6, 25), date(2017, 1, 3)))
    before = _bar_count(scope, taken)
    _yahoo(monkeypatch, {"ADPT": _frame(_weekdays(date(2019, 6, 27), TODAY))})

    assert _run(scope).unpriced == 1
    assert _bar_count(scope, taken) == before
    assert _get(scope, event).instrument_id is None


def test_stored_bars_that_start_late_are_completed(scope, monkeypatch):
    """A partial fetch of the same security (a daily poll that began later) is not a conflict."""
    event = _event(scope, "CDAK")
    instrument_id = _store_bars(scope, "CDAK", _weekdays(date(2023, 1, 2), TODAY))
    days = _weekdays(IPO, TODAY)
    _yahoo(monkeypatch, {"CDAK": _frame(days)})

    assert _run(scope).resolved_yahoo == 1
    assert _get(scope, event).instrument_id == instrument_id
    assert _bar_count(scope, instrument_id) == len(days)


def test_stored_bars_price_a_listing_with_no_provider_call(scope, monkeypatch):
    event = _event(scope, "CDAK")
    instrument_id = _store_bars(scope, "CDAK", _weekdays(IPO, TODAY))
    calls = _yahoo(monkeypatch, {})

    report = _run(scope, max_yahoo=0)

    assert (report.already, report.deferred) == (1, 0)
    assert calls == []
    row = _get(scope, event)
    assert (row.instrument_id, row.resolution) == (instrument_id, "yahoo")
    assert _instrument(scope, instrument_id).listed_on == IPO


def test_stored_tiingo_bars_price_a_listing_with_no_provider_call(scope, monkeypatch):
    event = _event(scope, "CDAK")
    end = date(2023, 9, 18)
    instrument_id = _store_bars(
        scope, "CDAKQ", _weekdays(IPO, end), source="tiingo", exchange="PINK"
    )
    listings = {"CDAKQ": [_listing("CDAKQ", IPO, end, "PINK")]}
    connector, calls = _tiingo(monkeypatch, scope, {})

    report = _run(scope, tiingo=connector, tiingo_listings=listings)

    assert report.already == 1
    assert calls == []
    assert _get(scope, event).resolution == "tiingo:CDAKQ"
    assert _get(scope, event).instrument_id == instrument_id


@pytest.mark.parametrize(
    ("symbol", "name", "reason"),
    [
        ("CONXU", "CONX Corp.", "unit"),
        ("HLXA", "Helix Acquisition Corp", "spac"),
        (None, "Meey Global Corp", "no-symbol"),
    ],
)
def test_excluded_rows_cost_no_provider_call(scope, monkeypatch, symbol, name, reason):
    event = _event(scope, symbol, name=name, price=10.0)
    calls = _yahoo(monkeypatch, {})

    report = _run(scope)

    assert report.excluded == {reason: 1}
    assert calls == []
    row = _get(scope, event)
    assert (row.resolution, row.instrument_id) == (f"excluded:{reason}", None)


def test_a_ticker_tiingo_files_as_an_etf_is_excluded(scope, monkeypatch):
    _event(scope, "BAR", name="GRANITESHARES GOLD", price=128.9)
    calls = _yahoo(monkeypatch, {})

    report = _run(scope, tiingo_listings={"BAR": [_listing("BAR", asset_type="ETF")]})

    assert report.excluded == {"fund": 1}
    assert calls == []


def test_a_shares_only_spac_is_caught_by_how_it_trades(scope, monkeypatch):
    """Finnhub labels ARYA with the name it merged into; its trust still pins it to $10."""
    event = _event(scope, "ARYA", name="Nautilus Biotechnology, Inc.", price=10.0)
    days = _weekdays(IPO, TODAY)
    _yahoo(monkeypatch, {"ARYA": _frame(days, [10.0 + 0.01 * (n % 2) for n in range(len(days))])})

    report = _run(scope)

    assert report.excluded == {"spac-like": 1}
    assert _get(scope, event).resolution == "excluded:spac-like"
    with scope() as session:
        assert session.scalar(select(func.count()).select_from(PriceBar)) == 0


def test_a_ten_dollar_company_that_trades_like_one_is_priced(scope, monkeypatch):
    event = _event(scope, "GLOB", name="GLOBANT S.A.", price=10.0)
    _yahoo(monkeypatch, {"GLOB": _frame(_weekdays(IPO, TODAY))})

    assert _run(scope).resolved_yahoo == 1
    assert _get(scope, event).resolution == "yahoo"


def test_a_ten_dollar_deal_too_new_to_judge_is_deferred(scope, monkeypatch):
    ipo = TODAY - timedelta(days=21)
    event = _event(scope, "NEWC", ipo=ipo, name="New Co", price=10.0)
    _yahoo(monkeypatch, {"NEWC": _frame(_weekdays(ipo, TODAY))})

    assert _run(scope).deferred == 1
    assert _get(scope, event).resolution is None


def test_a_ten_dollar_deal_that_died_young_is_priced(scope, monkeypatch):
    """A trust does not die in a month, so a short series that ended is no SPAC."""
    end = IPO + timedelta(days=14)
    event = _event(scope, "GONE", name="Gone Inc.", price=10.0)
    _yahoo(monkeypatch, {"GONE": _frame(_weekdays(IPO, end))})

    assert _run(scope).resolved_yahoo == 1
    assert _get(scope, event).instrument_id is not None


def test_an_ipo_too_new_to_have_bars_is_deferred_without_a_call(scope, monkeypatch):
    event = _event(scope, "TRXB", ipo=TODAY - timedelta(days=3))
    calls = _yahoo(monkeypatch, {})

    assert _run(scope).deferred == 1
    assert calls == []
    assert _get(scope, event).resolution is None


def test_the_yahoo_budget_defers_live_listings_but_not_dead_ones(scope, monkeypatch):
    live = _event(scope, "LIVE", name="Live Inc.")
    dead = _event(scope, "DEAD", name="Dead Inc.")
    end = date(2022, 1, 3)
    listings = {"DEAD": [_listing("DEAD", IPO, end)], "LIVE": [_listing("LIVE")]}
    calls = _yahoo(monkeypatch, {})
    connector, _ = _tiingo(monkeypatch, scope, {"DEAD": _weekdays(IPO, end)})

    report = _run(scope, max_yahoo=0, tiingo=connector, tiingo_listings=listings)

    assert (report.deferred, report.resolved_tiingo) == (1, 1)
    assert calls == []
    assert _get(scope, live).resolution is None
    assert _get(scope, dead).resolution == "tiingo:DEAD"


def test_a_spent_tiingo_budget_defers(scope, monkeypatch):
    event = _event(scope, "DEAD", name="Dead Inc.")
    end = date(2022, 1, 3)
    usage = TiingoUsage(monthly_symbols=0)
    connector, calls = _tiingo(monkeypatch, scope, {"DEAD": _weekdays(IPO, end)}, usage=usage)

    report = _run(scope, tiingo=connector, tiingo_listings={"DEAD": [_listing("DEAD", IPO, end)]})

    assert report.deferred == 1
    assert calls == []
    assert _get(scope, event).resolution is None


def test_a_refused_tiingo_key_fails_the_row_and_blocks_the_rest(scope, monkeypatch):
    end = date(2022, 1, 3)
    _event(scope, "DEDA", name="Dead A Inc.")
    _event(scope, "DEDB", name="Dead B Inc.", ipo=IPO + timedelta(days=1))
    listings = {
        "DEDA": [_listing("DEDA", IPO, end)],
        "DEDB": [_listing("DEDB", IPO + timedelta(days=1), end)],
    }
    connector, calls = _tiingo(monkeypatch, scope, {"DEDA": 401, "DEDB": 401})

    with pytest.raises(RuntimeError, match="all 1 IPO"):
        _run(scope, tiingo=connector, tiingo_listings=listings)
    assert calls == ["DEDA"]


def test_yahoo_errors_are_reported_and_the_run_goes_on(scope, monkeypatch):
    _event(scope, "BADY", name="Bad Inc.")
    good = _event(scope, "GOOD", name="Good Inc.")
    _yahoo(
        monkeypatch,
        {"BADY": RuntimeError("rate limited"), "GOOD": _frame(_weekdays(IPO, TODAY))},
    )

    report = _run(scope)

    assert list(report.failed) == [f"BADY@{IPO.isoformat()}"]
    assert _get(scope, good).resolution == "yahoo"


def test_a_run_where_every_provider_call_failed_raises(scope, monkeypatch):
    _event(scope, "BADY", name="Bad Inc.")
    _yahoo(monkeypatch, {"BADY": RuntimeError("rate limited")})

    with pytest.raises(RuntimeError, match="all 1 IPO"):
        _run(scope)


def test_only_completed_calendar_deals_in_the_window_are_priced(scope, monkeypatch):
    _event(scope, "EDGR", source="sec_edgar")
    _event(scope, "EXPD", stage="expected")
    _event(scope, "OLDD", ipo=date(2012, 12, 31))
    _event(scope, "FUTR", ipo=TODAY + timedelta(days=1))
    calls = _yahoo(monkeypatch, {})

    report = _run(scope)

    assert report == type(report)()
    assert calls == []


def test_unpriced_rows_are_retried_after_a_while_and_excluded_ones_never(scope, monkeypatch):
    now = datetime.now(UTC)
    recent = _event(scope, "RCNT", resolution="unpriced", resolved_at=now - timedelta(days=1))
    stale = _event(
        scope,
        "STAL",
        name="Stale Inc.",
        resolution="unpriced",
        resolved_at=now - timedelta(days=60),
    )
    _event(
        scope,
        "EXCL",
        name="Excl Inc.",
        resolution="excluded:spac",
        resolved_at=now - timedelta(days=600),
    )
    calls = _yahoo(monkeypatch, {"STAL": _frame(_weekdays(IPO, TODAY))})

    report = _run(scope)

    assert report.resolved_yahoo == 1
    assert [call[0] for call in calls] == ["STAL"]
    assert _get(scope, stale).resolution == "yahoo"
    assert _get(scope, recent).resolution == "unpriced"


def test_a_second_run_is_a_no_op(scope, monkeypatch):
    _event(scope, "CDAK")
    calls = _yahoo(monkeypatch, {"CDAK": _frame(_weekdays(IPO, TODAY))})
    _run(scope)

    report = _run(scope)

    assert report == type(report)()
    assert len(calls) == 1
