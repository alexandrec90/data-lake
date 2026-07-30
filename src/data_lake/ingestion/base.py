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
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from data_lake.runtime import SessionFactory, resolve_session_factory, resolve_settings
from data_lake.settings import LakeSettings

__all__ = ["Connector", "SessionFactory", "resolve_session_factory", "stable_hash"]


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
