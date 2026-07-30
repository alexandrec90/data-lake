"""What this package needs from a consumer's configuration object — and nothing more.

The package does not own a config system. A consumer passes in whatever settings object it
already has; these Protocols describe the attributes the connectors and the archive actually
read, so the consumer's class satisfies them **structurally** — no base class to inherit, no
import from this package in the consumer's config module.

``ibkr_trader.config.Settings`` (a pydantic ``BaseSettings``) satisfies :class:`LakeSettings`
as-is, which is the point: extracting this package required no change to it.

Members are declared as read-only properties on purpose. A Protocol's plain *attributes* are
invariant, so ``archive_backend: str`` here would reject a consumer whose attribute is typed
``Literal["none", "local", "s3"]``. Properties are covariant and read-only, and an ordinary
mutable attribute satisfies them — which is what every real settings class has.
"""

from typing import Literal, Protocol, runtime_checkable


@runtime_checkable
class IngestionSettings(Protocol):
    """Provider credentials and per-source knobs read by ``data_lake.ingestion``."""

    @property
    def newsapi_key(self) -> str: ...

    @property
    def finnhub_key(self) -> str: ...

    @property
    def alpha_vantage_key(self) -> str: ...

    @property
    def fmp_key(self) -> str: ...

    @property
    def reddit_client_id(self) -> str: ...

    @property
    def reddit_client_secret(self) -> str: ...

    @property
    def reddit_user_agent(self) -> str: ...

    @property
    def subreddits(self) -> list[str]: ...

    @property
    def ibkr_host(self) -> str: ...

    @property
    def ibkr_port(self) -> int: ...

    @property
    def ibkr_client_id(self) -> int: ...


@runtime_checkable
class ArchiveSettings(Protocol):
    """Object-storage location read by ``data_lake.archive``.

    ``archive_backend`` selects the store: ``"none"`` disables archiving, ``"local"`` uses
    ``archive_local_dir``, ``"s3"`` uses the ``archive_s3_*`` fields (Cloudflare R2 wants
    ``archive_s3_region = "auto"``).
    """

    @property
    def archive_backend(self) -> Literal["none", "local", "s3"]: ...

    @property
    def archive_local_dir(self) -> str: ...

    @property
    def archive_s3_bucket(self) -> str: ...

    @property
    def archive_s3_endpoint_url(self) -> str: ...

    @property
    def archive_s3_region(self) -> str: ...

    @property
    def archive_s3_access_key_id(self) -> str: ...

    @property
    def archive_s3_secret_access_key(self) -> str: ...

    @property
    def archive_s3_prefix(self) -> str: ...


@runtime_checkable
class LakeSettings(IngestionSettings, ArchiveSettings, Protocol):
    """Everything the package reads. What :func:`data_lake.configure` accepts."""
