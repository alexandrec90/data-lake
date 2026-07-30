"""Suite-wide package configuration.

Every test runs against a configured package, the way a real consumer process is configured
once at startup — but with a settings object the test owns (``_lake_env.SETTINGS``) and no
session factory, so anything that forgets to inject one fails loudly instead of reaching for a
database. Connector tests pass ``session_factory=`` explicitly; that injection always wins.
"""

import pytest

import data_lake
from _lake_env import SETTINGS


@pytest.fixture(autouse=True)
def _configured_lake():
    data_lake.configure(settings=SETTINGS)
    yield
    data_lake.reset()
