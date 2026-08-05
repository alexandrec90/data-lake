"""Adopting part of the shared schema: the filter every consumer eventually needs.

Importing :mod:`data_lake.db.models` registers the package's **entire** schema on the one
shared ``Base.metadata``. That single metadata object is deliberate — it is what lets a
consumer's Alembic see its own private tables and the lake's together, in one autogenerate
pass — but it has a consequence that surprises every new consumer:

    Seeing the whole schema is not the same as wanting all of it.

A housing project consuming this package inherits ``instruments``, ``price_bars``,
``dividends``, ``fundamental_snapshots``, ``news_articles`` and the rest. Alembic compares
the *whole* metadata against the database, so with no filter its first
``revision --autogenerate`` proposes creating a dozen empty market-data tables, and
``alembic check`` never comes back clean.

:func:`include_only` is the fix, and it lives here rather than in any one consumer so the
next project does not have to rediscover it::

    # migrations/env.py
    from data_lake.db.adoption import include_only

    include_object = include_only({"listings"})

    context.configure(..., target_metadata=Base.metadata, include_object=include_object)

The filter is applied to **both** sides of the comparison, which is the property that makes
it safe: an unadopted table is never created from the metadata, and never proposed for
deletion because it is missing from the database.

This is an *adoption list*, not a denylist. Naming a table is how a consumer says "I keep
this one" — a deliberate act, and the reason the default is to adopt nothing.
"""

from collections.abc import Callable, Iterable

__all__ = ["include_only"]


def include_only(tables: Iterable[str]) -> Callable[..., bool]:
    """Build an Alembic ``include_object`` hook admitting only ``tables``.

    Child objects — columns, indexes, constraints — inherit their table's decision, so a
    partially-adopted schema never yields a half-migrated table. Objects with no parent
    table (schemas, for instance) are always admitted: the adoption list says nothing
    about them, and silently dropping them would be a surprise of a different kind.
    """
    adopted = frozenset(tables)

    def include_object(obj, name, type_, reflected, compare_to) -> bool:
        if type_ == "table":
            return name in adopted
        parent = getattr(obj, "table", None)
        if parent is not None:
            return parent.name in adopted
        return True

    return include_object
