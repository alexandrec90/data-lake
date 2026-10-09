from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from _db import make_session_scope
from data_lake.db.models import IpoEvent
from data_lake.ingestion.market.ipo_common import upsert_ipo_event

T0 = datetime(2026, 10, 1, tzinfo=UTC)
T1 = datetime(2026, 10, 2, tzinfo=UTC)


def _fields(**overrides) -> dict:
    return {"company_name": "Acme", "stage": "filed", "raw": {"a": 1}, **overrides}


def test_insert_then_refresh_never_moves_first_seen_or_blanks_a_field():
    session_cm = make_session_scope()
    with session_cm() as session:
        assert upsert_ipo_event(session, "src", "k", _fields(price_low=10.0), T0) is True
        assert (
            upsert_ipo_event(
                session, "src", "k", _fields(stage="priced", price_low=None, raw={"b": 2}), T1
            )
            is False
        )

    with session_cm() as session:
        [row] = session.scalars(select(IpoEvent)).all()
        assert row.stage == "priced"
        assert row.price_low == 10.0  # None does not overwrite
        assert row.raw == {"a": 1, "b": 2}  # merged, not replaced
        assert row.first_seen_at.replace(tzinfo=UTC) == T0
        assert row.fetched_at.replace(tzinfo=UTC) == T1


def test_a_row_added_earlier_in_the_batch_is_found_again():
    session_cm = make_session_scope()
    with session_cm() as session:
        upsert_ipo_event(session, "src", "k", _fields(), T0)
        assert upsert_ipo_event(session, "src", "k", _fields(), T0) is False
        assert upsert_ipo_event(session, "other", "k", _fields(), T0) is True


@pytest.mark.parametrize("stage", [None, "rumoured", "FILED"])
def test_stage_outside_the_vocabulary_raises(stage):
    with make_session_scope()() as session, pytest.raises(ValueError, match="stage"):
        upsert_ipo_event(session, "src", "k", _fields(stage=stage), T0)


@pytest.mark.parametrize("column", ["first_seen_at", "source", "account_id"])
def test_identity_and_unknown_columns_are_refused(column):
    with make_session_scope()() as session, pytest.raises(ValueError, match=column):
        upsert_ipo_event(session, "src", "k", _fields(**{column: "x"}), T0)
