"""The composition root: where a consumer hands the package its settings and its database.

This package owns no config system and no SQLAlchemy engine. It needs both, so the consumer
supplies them — once, at its own entry point:

    import data_lake
    from ibkr_trader.config import get_settings
    from ibkr_trader.db.session import get_session

    data_lake.configure(settings=get_settings(), session_factory=get_session)

Everything downstream (connectors, the archive store) then resolves through :func:`resolve_settings`
and :func:`resolve_session_factory`, and per-call injection still wins over these defaults —
``Connector(settings=..., session_factory=...)`` is what tests use, and it never touches the
globals.

These *are* process-wide globals, which is normally worth avoiding. The alternative is threading
a settings object and a session factory through every connector construction site in the
consumer; a single explicit ``configure()`` at startup buys the same inversion for one line.
The rule that keeps it honest: nothing inside this package ever calls ``configure()``, only
reads what the consumer set. Tests use :func:`reset` (or plain injection) to stay isolated.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager

from sqlalchemy.orm import Session

from data_lake.settings import LakeSettings

#: Zero-arg callable yielding a transactional ``Session`` context manager (commit on clean exit).
SessionFactory = Callable[[], AbstractContextManager[Session]]

_settings: LakeSettings | None = None
_session_factory: SessionFactory | None = None

_UNCONFIGURED = (
    "data_lake has no process-wide {what}. Either call "
    "data_lake.configure({arg}=...) once at your application's entry point, or pass "
    "{arg}= explicitly to the connector or archive call."
)


def configure(
    *,
    settings: LakeSettings | None = None,
    session_factory: SessionFactory | None = None,
) -> None:
    """Set the process-wide defaults. Call once, at the consumer's entry point.

    Either argument may be omitted to leave that default untouched, so a consumer that only
    uses the archive need never supply provider credentials.
    """
    global _settings, _session_factory
    if settings is not None:
        _settings = settings
    if session_factory is not None:
        _session_factory = session_factory


def reset() -> None:
    """Forget both defaults. For tests — never call this from library code."""
    global _settings, _session_factory
    _settings = None
    _session_factory = None


def resolve_settings(settings: LakeSettings | None) -> LakeSettings:
    """The injected settings, else the configured process-wide ones."""
    if settings is not None:
        return settings
    if _settings is None:
        raise RuntimeError(_UNCONFIGURED.format(what="settings", arg="settings"))
    return _settings


def resolve_session_factory(session_factory: SessionFactory | None) -> SessionFactory:
    """The injected session factory, else the configured process-wide one.

    A session factory is any zero-arg callable returning a context manager that yields a
    SQLAlchemy ``Session`` and commits on clean exit. The consumer owns the engine; this
    package only borrows sessions from it.
    """
    if session_factory is not None:
        return session_factory
    if _session_factory is None:
        raise RuntimeError(_UNCONFIGURED.format(what="session factory", arg="session_factory"))
    return _session_factory
