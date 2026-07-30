"""How the package gets its settings and its database — the whole inversion, in one file.

The rules under test: per-call injection beats the configured default; an unconfigured process
fails with an actionable error instead of reaching for someone else's database; and the
resolution happens once, lazily, so importing a connector never touches config or an engine.
"""

import contextlib
import subprocess
import sys
import textwrap

import pytest

import data_lake
from data_lake.ingestion.base import Connector
from data_lake.runtime import resolve_session_factory, resolve_settings
from data_lake.testing import lake_settings


class _Probe(Connector):
    name = "probe"

    def fetch(self, **kwargs) -> int:
        return 0


@contextlib.contextmanager
def _fake_session():
    yield "session"


@pytest.fixture(autouse=True)
def _unconfigured():
    """Override conftest's configured package — these tests set up their own state."""
    data_lake.reset()
    yield
    data_lake.reset()


def test_injected_settings_beat_the_configured_default():
    data_lake.configure(settings=lake_settings(finnhub_key="configured"))
    injected = lake_settings(finnhub_key="injected")

    assert _Probe(settings=injected).settings is injected


def test_falls_back_to_the_configured_settings():
    configured = lake_settings(finnhub_key="configured")
    data_lake.configure(settings=configured)

    assert _Probe().settings is configured


def test_unconfigured_settings_raise_an_actionable_error():
    with pytest.raises(RuntimeError, match=r"data_lake\.configure\(settings=\.\.\.\)"):
        _ = _Probe().settings


def test_unconfigured_session_factory_raises_an_actionable_error():
    with pytest.raises(RuntimeError, match=r"data_lake\.configure\(session_factory=\.\.\.\)"):
        _Probe().session()


def test_injected_session_factory_beats_the_configured_default():
    data_lake.configure(session_factory=lambda: pytest.fail("configured factory was used"))

    with _Probe(session_factory=_fake_session).session() as session:
        assert session == "session"


def test_resolution_is_cached_so_reconfiguring_mid_life_does_not_swap_a_connector():
    """A connector resolves once: a later ``configure`` must not move its database."""
    data_lake.configure(settings=lake_settings(fmp_key="first"), session_factory=_fake_session)
    connector = _Probe()
    assert connector.settings.fmp_key == "first"

    data_lake.configure(settings=lake_settings(fmp_key="second"))

    assert connector.settings.fmp_key == "first"
    assert _Probe().settings.fmp_key == "second"


def test_configure_leaves_the_argument_it_was_not_given():
    data_lake.configure(settings=lake_settings(fmp_key="kept"), session_factory=_fake_session)
    data_lake.configure(session_factory=_fake_session)

    assert resolve_settings(None).fmp_key == "kept"


def test_reset_forgets_both():
    data_lake.configure(settings=lake_settings(), session_factory=_fake_session)
    data_lake.reset()

    with pytest.raises(RuntimeError):
        resolve_settings(None)
    with pytest.raises(RuntimeError):
        resolve_session_factory(None)


def test_importing_every_connector_pulls_in_no_provider_sdk_and_no_engine():
    """The reason the fallbacks are lazy: importing the tree must stay cheap and side-effect free.

    A subprocess, because the rest of the suite has already imported these modules.
    """
    script = textwrap.dedent("""
        import pkgutil, importlib, sys
        import data_lake.ingestion as ing
        for m in pkgutil.walk_packages(ing.__path__, "data_lake.ingestion."):
            importlib.import_module(m.name)
        heavy = [n for n in ("praw", "pytrends", "yfinance", "boto3", "duckdb")
                 if n in sys.modules]
        print(",".join(heavy))
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == ""
