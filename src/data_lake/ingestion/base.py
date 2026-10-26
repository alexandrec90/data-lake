"""Connector contract shared by all data sources.

Connectors are deliberately dumb: fetch from one provider, upsert into the consumer's
Postgres, return a count. No feature engineering here. Idempotency comes from the unique
constraints in :mod:`data_lake.db.models` — always upsert, never blind-insert.

Both of a connector's ambient dependencies are *injected*, not reached for:

- **Configuration** — pass a settings object satisfying :class:`~data_lake.settings.LakeSettings`
  to the constructor, or let ``self.settings`` fall back to whatever the consumer handed
  :func:`data_lake.configure`.
- **Database access** — pass a ``session_factory`` (any zero-arg callable returning a context
  manager that yields a SQLAlchemy ``Session`` and commits on clean exit), or let
  ``self.session()`` fall back to the configured one.

This package owns neither a config system nor an engine, so there is nothing to reach for if
the consumer configured neither: an unconfigured process raises a ``RuntimeError`` naming the
fix rather than silently opening someone else's database. See :mod:`data_lake.runtime`.

Module-level helpers in the connector tree take the same ``session_factory`` argument and
resolve it through :func:`resolve_session_factory`.
"""

import abc
import hashlib
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy.orm import Session

from data_lake.runtime import SessionFactory, resolve_session_factory, resolve_settings
from data_lake.settings import LakeSettings

__all__ = [
    "Connector",
    "SessionFactory",
    "pseudonymize",
    "resolve_session_factory",
    "stable_hash",
]


class Connector(abc.ABC):
    """One external data source."""

    #: short identifier stored in the `source`/`platform` columns
    name: str = "override-me"

    def __init__(
        self,
        settings: LakeSettings | None = None,
        session_factory: SessionFactory | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    @property
    def settings(self) -> LakeSettings:
        """The injected settings, else the configured ones (resolved once, on first use)."""
        if self._settings is None:
            self._settings = resolve_settings(None)
        return self._settings

    @property
    def session_factory(self) -> SessionFactory:
        """The injected session factory, else the configured one (resolved once, on first use)."""
        if self._session_factory is None:
            self._session_factory = resolve_session_factory(None)
        return self._session_factory

    def session(self) -> AbstractContextManager[Session]:
        """Open one short-lived transactional session: ``with self.session() as session:``."""
        return self.session_factory()

    @abc.abstractmethod
    def fetch(self, **kwargs) -> int:
        """Pull new data and persist it. Returns number of rows upserted."""


def stable_hash(value: str) -> str:
    """sha256 hex digest — used for author pseudonymization and URL-based external ids."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pseudonymize(payload: Mapping[str, Any], person_fields: Iterable[str]) -> dict[str, Any]:
    """A copy of a provider payload that is safe to keep whole in a ``raw`` column.

    Each top-level field in ``person_fields`` is replaced by ``<field>_hash`` (a
    :func:`stable_hash` digest, or ``None`` when the provider sent nothing), so the stored
    payload still says *that* two items share an author without saying *who*. Everything
    else passes through untouched — this is what lets a connector keep the full record for
    reprocessing instead of guessing up front which fields will matter.
    """
    safe = dict(payload)
    for name in person_fields:
        if name in safe:
            value = safe.pop(name)
            safe[f"{name}_hash"] = stable_hash(str(value)) if value else None
    return safe
