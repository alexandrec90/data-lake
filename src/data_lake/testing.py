"""A settings stub for tests — this package's and yours.

The package deliberately owns no config class, which leaves tests with nothing to instantiate.
:func:`lake_settings` fills that gap: a plain object carrying every attribute in
:class:`~data_lake.settings.LakeSettings`, with the same defaults a real consumer would have,
and keyword overrides for the handful a given test cares about::

    store = store_from_settings(lake_settings(archive_backend="local", archive_local_dir=tmp))

Importable without any extra, and it never reads the environment — a test that forgets to
override a credential gets an obviously-fake value, not whatever is in your shell.
"""

from dataclasses import dataclass, field, fields
from typing import Any, Literal

__all__ = ["FakeLakeSettings", "lake_settings"]


def _subreddits() -> list[str]:
    return ["wallstreetbets", "investing", "stocks", "CanadianInvestor"]


@dataclass
class FakeLakeSettings:
    """Structurally satisfies :class:`~data_lake.settings.LakeSettings`."""

    # ingestion
    newsapi_key: str = "test-newsapi-key"
    finnhub_key: str = "test-finnhub-key"
    alpha_vantage_key: str = "test-alpha-vantage-key"
    fmp_key: str = "test-fmp-key"
    reddit_client_id: str = "test-reddit-id"
    reddit_client_secret: str = "test-reddit-secret"
    reddit_user_agent: str = "data-lake-tests/0.1"
    subreddits: list[str] = field(default_factory=_subreddits)
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4004
    ibkr_client_id: int = 1

    # archive
    archive_backend: Literal["none", "local", "s3"] = "none"
    archive_local_dir: str = "archive"
    archive_s3_bucket: str = ""
    archive_s3_endpoint_url: str = ""
    archive_s3_region: str = "auto"
    archive_s3_access_key_id: str = ""
    archive_s3_secret_access_key: str = ""
    archive_s3_prefix: str = ""


def lake_settings(**overrides: Any) -> FakeLakeSettings:
    """Build a :class:`FakeLakeSettings`, raising on a misspelled field name.

    ``FakeLakeSettings(**overrides)`` would already raise, but its ``TypeError`` does not say
    which names *are* valid — worth the few lines here, since a typo'd override otherwise reads
    as "the code ignored my setting".
    """
    known = {f.name for f in fields(FakeLakeSettings)}
    unknown = set(overrides) - known
    if unknown:
        raise TypeError(
            f"unknown settings field(s) {sorted(unknown)}; valid fields are {sorted(known)}"
        )
    return FakeLakeSettings(**overrides)
