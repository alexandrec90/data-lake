"""The partial-schema adoption filter.

The failure this guards is silent in both directions. Too broad and a consumer's database
grows a dozen empty tables from someone else's domain; too narrow and autogenerate proposes
*dropping* the table it actually depends on.
"""

from data_lake.db.adoption import include_only
from data_lake.db.base import Base
from data_lake.db.models import Listing, PriceBar


class FakeChild:
    """A column/index-like object, which Alembic identifies by its parent table."""

    def __init__(self, table):
        self.table = table


def test_adopted_table_is_included():
    assert include_only({"listings"})(None, "listings", "table", False, None) is True


def test_unadopted_tables_are_excluded():
    include = include_only({"listings"})
    for name in ("price_bars", "instruments", "news_articles", "social_posts"):
        assert include(None, name, "table", False, None) is False, name


def test_adopting_nothing_excludes_everything():
    # The default posture: a consumer opts in, table by table.
    include = include_only(set())
    for name in Base.metadata.tables:
        assert include(None, name, "table", False, None) is False, name


def test_children_inherit_their_tables_decision():
    include = include_only({"listings"})
    listings = Base.metadata.tables[Listing.__tablename__]
    bars = Base.metadata.tables[PriceBar.__tablename__]
    assert include(FakeChild(listings), "price_cad", "column", False, None) is True
    assert include(FakeChild(listings), "ix_listings_posted_at", "index", False, None) is True
    assert include(FakeChild(bars), "close", "column", False, None) is False


def test_parentless_objects_are_admitted():
    # Alembic asks about schema-level objects with no `.table`. The adoption list says
    # nothing about those, so excluding them would be a surprise of a different kind.
    assert include_only(set())(object(), "public", "schema", False, None) is True


def test_accepts_any_iterable_and_snapshots_it():
    names = ["listings"]
    include = include_only(names)
    names.append("price_bars")  # must not retroactively widen the filter
    assert include(None, "price_bars", "table", False, None) is False


def test_a_consumer_can_adopt_several_tables():
    include = include_only({"news_articles", "social_posts"})
    assert include(None, "news_articles", "table", False, None) is True
    assert include(None, "social_posts", "table", False, None) is True
    assert include(None, "listings", "table", False, None) is False
