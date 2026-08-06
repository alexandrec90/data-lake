"""The ``listings`` table.

It is here to prove a point as much as to test a schema: the lake is domain-agnostic. This
table shares nothing with the market/news/social ones around it — different subject, different
consumer, different upsert key — and it belongs in the package all the same, because the
boundary the seam guards is *account-shape*, not subject matter.

Written by ``apt-finder``; the assertions below are the contract that consumer relies on.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data_lake.db.base import Base
from data_lake.db.models import Listing
from data_lake.ingestion.base import stable_hash


@pytest.fixture
def session():
    """In-memory SQLite, like every other table test here — no container required."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def make(**overrides) -> Listing:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    base = {
        "platform": "kijiji",
        "external_id": "1700123456",
        "title": "Beau 4 1/2 à Gatineau",
        "price_cad": Decimal("1450.00"),
        "price_period": "month",
        "city": "gatineau",
        "province": "QC",
        "verdict": "match",
        "first_seen_at": now,
        "last_seen_at": now,
    }
    return Listing(**{**base, **overrides})


def test_round_trips(session):
    session.add(make())
    session.commit()

    stored = session.scalar(select(Listing))
    assert stored.platform == "kijiji"
    assert stored.province == "QC"
    assert stored.verdict == "match"


def test_price_stays_exact(session):
    # Numeric, not Float. Rent is money; 1499.99 must not come back 1499.9899999.
    session.add(make(external_id="p1", price_cad=Decimal("1499.99")))
    session.commit()
    assert session.scalar(select(Listing.price_cad)) == Decimal("1499.99")


def test_platform_and_external_id_are_unique_together(session):
    session.add(make())
    session.commit()
    session.add(make())
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_external_id_on_two_platforms_is_allowed(session):
    # Kijiji and Facebook mint ids independently, so the key must be the pair.
    session.add(make(platform="kijiji", external_id="7"))
    session.add(make(platform="facebook", external_id="7"))
    session.commit()
    assert len(session.execute(select(Listing)).scalars().all()) == 2


def test_appliance_columns_are_tri_state(session):
    # Not nullable booleans: "the ad says no washer" and "the ad never mentions one" are
    # different facts, and a consumer's filter depends on telling them apart.
    session.add(make(washer="present", dryer="absent", fridge="unknown"))
    session.commit()
    stored = session.scalar(select(Listing))
    assert (stored.washer, stored.dryer, stored.fridge) == ("present", "absent", "unknown")


def test_seller_is_stored_as_a_hash(session):
    # Québec Law 25: the name never reaches the database.
    session.add(make(seller_hash=stable_hash("Jean Tremblay")))
    session.commit()
    stored = session.scalar(select(Listing))
    assert len(stored.seller_hash) == 64
    assert "Jean" not in stored.seller_hash


def test_raw_payload_and_reasons_round_trip_as_json(session):
    session.add(make(raw={"id": "1", "nested": {"a": [1, 2]}}, verdict_reasons=["over_budget"]))
    session.commit()
    stored = session.scalar(select(Listing))
    assert stored.raw["nested"]["a"] == [1, 2]
    assert stored.verdict_reasons == ["over_budget"]


def test_optional_columns_default_to_null(session):
    session.add(make())
    session.commit()
    stored = session.scalar(select(Listing))
    for column in ("url", "description", "latitude", "rooms", "washer", "raw", "seller_hash"):
        assert getattr(stored, column) is None, column


def test_verdict_vocabulary_is_not_constrained_by_the_lake(session):
    """The lake stores the judgement; it does not define it.

    apt-finder uses match/maybe/reject. A second consumer with different criteria must be
    able to write its own words without a migration here — so this column is a plain string
    with no CHECK constraint or enum, and that is deliberate.
    """
    session.add(make(external_id="v1", verdict="shortlist"))
    session.commit()
    assert session.scalar(select(Listing.verdict)) == "shortlist"


def test_listings_is_a_lake_table():
    # The seam tests parametrize over Base.metadata; this asserts the table is actually in
    # it, which is also what makes the dataset eligible for the shared archive.
    assert "listings" in Base.metadata.tables
