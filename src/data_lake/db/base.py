"""Declarative base and column-type helpers shared by every model module.

This module is deliberately tiny and dependency-free: it is the seam the future ``data-lake``
package (docs/plans/active/data-lake.md, Phase 2) is built on. Both :mod:`lake_models` and
:mod:`trading_models` map onto this one ``Base``, so a single ``Base.metadata`` still drives
Alembic autogenerate even though the tables live in two modules.
"""

from sqlalchemy import JSON, BigInteger, Integer
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


SqliteFriendlyBigInt = BigInteger().with_variant(Integer, "sqlite")

#: JSONB on Postgres (indexable, typed), plain JSON on SQLite (test DB has no JSONB).
JsonVariant = JSON().with_variant(JSONB(), "postgresql")
