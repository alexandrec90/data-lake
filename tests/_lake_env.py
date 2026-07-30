"""The one settings object the suite configures the package with.

Connectors used to read credentials from the environment via the consumer's pydantic settings,
so tests set them with ``monkeypatch.setenv``. The package has no config system and never
reads the environment, so tests now set the attribute directly:

    monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")

``monkeypatch`` restores it after the test, exactly as it did for the env var. Credentials
start **empty** so the "missing key raises" tests get an unset key without doing anything, and
— unlike the env-var approach — a real ``FINNHUB_KEY`` exported in the developer's shell can no
longer leak into a test run.
"""

from data_lake.testing import lake_settings

SETTINGS = lake_settings(
    newsapi_key="",
    finnhub_key="",
    alpha_vantage_key="",
    fmp_key="",
    reddit_client_id="",
    reddit_client_secret="",
)
