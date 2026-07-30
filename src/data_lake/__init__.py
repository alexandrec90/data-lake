"""Shared ingestion + cold-storage layer.

Two halves, usable independently:

- :mod:`data_lake.ingestion` — one connector per provider (news, social, market). Each fetches
  and **upserts into the consumer's Postgres**; the consumer owns the engine and the schema.
- :mod:`data_lake.archive` — offload cold rows to Parquet on any S3-compatible bucket
  (Cloudflare R2) with verify-before-delete, a self-describing catalog under ``_catalog/``, and
  a read-only DuckDB lens. Needs the ``[archive]`` extra (``[research]`` for the lens).

The tables live in :mod:`data_lake.db.models`, which declares only shareable data — market,
corporate, news, social, features. Anything account-shaped (orders, executions, risk state)
belongs to the consumer and must never be defined here; that boundary is the whole reason this
package exists separately.

Wire it up once, at your entry point::

    import data_lake
    data_lake.configure(settings=my_settings, session_factory=my_session_factory)

``settings`` is any object with the attributes in :class:`~data_lake.settings.LakeSettings`
(structural — no base class to inherit). ``session_factory`` is any zero-arg callable returning
a context manager that yields a SQLAlchemy ``Session``. Per-call injection always wins over
these defaults, which is what tests use.
"""

from data_lake.runtime import SessionFactory, configure, reset
from data_lake.settings import ArchiveSettings, IngestionSettings, LakeSettings

__all__ = [
    "ArchiveSettings",
    "IngestionSettings",
    "LakeSettings",
    "SessionFactory",
    "configure",
    "reset",
]
