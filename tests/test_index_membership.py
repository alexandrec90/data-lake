from datetime import date

import pytest
from sqlalchemy import select

from _db import make_session_scope
from data_lake.db.models import IndexMembership
from data_lake.ingestion.market import index_membership as im


def _csv(rows: list[tuple[str, str, str]], filler: int = 0) -> str:
    """A start/end file; ``filler`` adds that many current members so the guard passes."""
    lines = ["ticker,start_date,end_date"]
    lines += [",".join(row) for row in rows]
    lines += [f"F{i:03d},2000-01-01," for i in range(filler)]
    return "\n".join(lines) + "\n"


def _rows(scope) -> dict[tuple[str, date], IndexMembership]:
    with scope() as session:
        return {
            (row.symbol, row.start_date): row
            for row in session.scalars(select(IndexMembership)).all()
            if not row.symbol.startswith("F")
        }


def test_parse_reads_spans_and_open_ends():
    spans = im.parse_start_end_csv(
        "ticker,start_date,end_date\nbrk.b,2010-02-16,\nSIVB,2018-03-19,2023-03-15\n\n,,\n"
    )
    assert spans == [
        im.MembershipSpan("BRK.B", date(2010, 2, 16), None),
        im.MembershipSpan("SIVB", date(2018, 3, 19), date(2023, 3, 15)),
    ]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("symbol,from,to\nA,2000-01-01,\n", "expected columns"),
        ("ticker,start_date,end_date\nA,01/02/2000,\n", "line 2: bad date"),
        ("ticker,start_date,end_date\nA,2000-01-01,\nA,2000-01-01,2001-01-01\n", "duplicate"),
    ],
)
def test_parse_rejects_malformed_files(text, match):
    with pytest.raises(ValueError, match=match):
        im.parse_start_end_csv(text)


def test_fetch_upserts_moves_end_dates_and_deletes_stale_rows(monkeypatch):
    scope = make_session_scope()
    files = [
        _csv(
            [("AAA", "2001-01-01", ""), ("BBB", "2002-01-01", ""), ("OLD", "1999-01-01", "")],
            filler=im.MIN_CURRENT_MEMBERS,
        ),
        _csv(
            [("AAA", "2001-01-01", ""), ("BBB", "2002-01-01", "2024-06-03")],
            filler=im.MIN_CURRENT_MEMBERS,
        ),
    ]
    urls: list[str] = []

    def fake_get(url):
        urls.append(url)
        return files.pop(0)

    monkeypatch.setattr(im, "_http_get_text", fake_get)
    connector = im.Sp500MembershipConnector(session_factory=scope)

    assert connector.fetch() == 3 + im.MIN_CURRENT_MEMBERS
    with scope() as session:
        aaa = session.scalar(select(IndexMembership).where(IndexMembership.symbol == "AAA"))
        assert aaa is not None
        aaa.resolution = "yahoo"  # pricing state must survive a refresh

    assert connector.fetch() == 2 + im.MIN_CURRENT_MEMBERS
    rows = _rows(scope)
    assert set(rows) == {("AAA", date(2001, 1, 1)), ("BBB", date(2002, 1, 1))}
    assert rows[("BBB", date(2002, 1, 1))].end_date == date(2024, 6, 3)
    assert rows[("AAA", date(2001, 1, 1))].resolution == "yahoo"
    assert all(r.index_code == "SP500" and r.source == "fja05680" for r in rows.values())
    assert urls == [im.SP500_START_END_URL] * 2


def test_fetch_leaves_other_sources_and_indexes_alone(monkeypatch):
    scope = make_session_scope()
    with scope() as session:
        session.add_all(
            [
                IndexMembership(
                    index_code="SP500", symbol="X", start_date=date(2000, 1, 1), source="other"
                ),
                IndexMembership(
                    index_code="NDX", symbol="Y", start_date=date(2000, 1, 1), source="fja05680"
                ),
            ]
        )
    monkeypatch.setattr(im, "_http_get_text", lambda url: _csv([], filler=im.MIN_CURRENT_MEMBERS))
    im.Sp500MembershipConnector(session_factory=scope).fetch()
    assert {("X", date(2000, 1, 1)), ("Y", date(2000, 1, 1))} <= set(_rows(scope))


def test_fetch_refuses_a_download_with_too_few_current_members(monkeypatch):
    scope = make_session_scope()
    monkeypatch.setattr(
        im, "_http_get_text", lambda url: _csv([("AAA", "2001-01-01", "")], filler=100)
    )
    with scope() as session:
        session.add(
            IndexMembership(
                index_code="SP500", symbol="KEEP", start_date=date(2000, 1, 1), source="fja05680"
            )
        )
    with pytest.raises(RuntimeError, match="101 current members"):
        im.Sp500MembershipConnector(session_factory=scope).fetch()
    assert set(_rows(scope)) == {("KEEP", date(2000, 1, 1))}
