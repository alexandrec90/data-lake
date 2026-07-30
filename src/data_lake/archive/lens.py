"""Read-only DuckDB views over archived Parquet datasets.

This module is a research convenience only.  Signals, backtests, and execution continue to
read Postgres; archived data must be restored before it enters any of those paths.  DuckDB
is optional and imported only when :func:`connect_lens` is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from data_lake.settings import ArchiveSettings

if TYPE_CHECKING:
    import duckdb


_EMPTY_BARS = """
SELECT
    CAST(NULL AS VARCHAR) AS symbol,
    CAST(NULL AS VARCHAR) AS exchange,
    CAST(NULL AS VARCHAR) AS currency,
    CAST(NULL AS TIMESTAMPTZ) AS ts,
    CAST(NULL AS VARCHAR) AS bar_size,
    CAST(NULL AS VARCHAR) AS source,
    CAST(NULL AS VARCHAR) AS what_to_show,
    CAST(NULL AS DOUBLE) AS open,
    CAST(NULL AS DOUBLE) AS high,
    CAST(NULL AS DOUBLE) AS low,
    CAST(NULL AS DOUBLE) AS close,
    CAST(NULL AS DOUBLE) AS volume
WHERE false
"""

_EMPTY_RAW_PAYLOADS = """
SELECT
    CAST(NULL AS VARCHAR) AS source,
    CAST(NULL AS VARCHAR) AS external_id,
    CAST(NULL AS TIMESTAMPTZ) AS event_ts,
    CAST(NULL AS TIMESTAMPTZ) AS fetched_at,
    CAST(NULL AS VARCHAR) AS raw_json
WHERE false
"""


def _require_duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "archive queries need the research extra — install with: uv sync --extra research"
        ) from exc
    return duckdb


def _sql_string(value: str) -> str:
    """Quote a trusted configuration value as a DuckDB string literal."""
    return "'" + value.replace("'", "''") + "'"


def _s3_root(settings: ArchiveSettings) -> str:
    if not settings.archive_s3_bucket:
        raise ValueError("S3 archive needs a bucket name (ARCHIVE_S3_BUCKET)")
    prefix = settings.archive_s3_prefix.strip("/")
    suffix = f"/{prefix}" if prefix else ""
    return f"s3://{settings.archive_s3_bucket}{suffix}"


def _endpoint_options(endpoint_url: str) -> list[str]:
    """Translate boto-style endpoint URLs into DuckDB secret options."""
    if not endpoint_url:
        return []
    parsed = urlsplit(endpoint_url if "://" in endpoint_url else f"https://{endpoint_url}")
    if not parsed.hostname:
        raise ValueError("ARCHIVE_S3_ENDPOINT_URL must contain a hostname")
    endpoint = parsed.hostname
    if parsed.port is not None:
        endpoint = f"{endpoint}:{parsed.port}"
    return [
        f"ENDPOINT {_sql_string(endpoint)}",
        f"USE_SSL {'true' if parsed.scheme.lower() == 'https' else 'false'}",
        "URL_STYLE 'path'",
    ]


def _configure_s3(connection: Any, settings: ArchiveSettings) -> str:
    """Load httpfs and create an in-memory, bucket-scoped S3 secret."""
    root = _s3_root(settings)
    connection.execute("INSTALL httpfs")
    connection.execute("LOAD httpfs")
    options = [
        "TYPE s3",
        "PROVIDER config",
        f"KEY_ID {_sql_string(settings.archive_s3_access_key_id)}",
        f"SECRET {_sql_string(settings.archive_s3_secret_access_key)}",
        f"REGION {_sql_string(settings.archive_s3_region or 'auto')}",
        *_endpoint_options(settings.archive_s3_endpoint_url),
        f"SCOPE {_sql_string(root)}",
    ]
    connection.execute("CREATE SECRET archive_lens (\n    " + ",\n    ".join(options) + "\n)")
    return root


def _create_parquet_view(connection: Any, name: str, glob: str) -> None:
    connection.execute(
        f"CREATE VIEW {name} AS SELECT * FROM read_parquet("
        f"{_sql_string(glob)}, union_by_name = true, hive_partitioning = false)"
    )


def _create_empty_view(connection: Any, name: str, query: str) -> None:
    connection.execute(f"CREATE VIEW {name} AS {query}")


def _remote_glob_has_files(connection: Any, glob: str) -> bool:
    row = connection.execute(f"SELECT EXISTS (SELECT 1 FROM glob({_sql_string(glob)}))").fetchone()
    return bool(row and row[0])


def _configure_remote_view(connection: Any, name: str, glob: str, empty_query: str) -> None:
    if _remote_glob_has_files(connection, glob):
        _create_parquet_view(connection, name, glob)
    else:
        _create_empty_view(connection, name, empty_query)


def _configure_local_views(connection: Any, root: Path) -> None:
    bars_glob = root / "price_bars" / "bar_size=*" / "*.parquet"
    raw_glob = root / "raw" / "*" / "*.parquet"
    if any(root.glob("price_bars/bar_size=*/*.parquet")):
        _create_parquet_view(connection, "bars", bars_glob.resolve().as_posix())
    else:
        _create_empty_view(connection, "bars", _EMPTY_BARS)
    if any(root.glob("raw/*/*.parquet")):
        _create_parquet_view(connection, "raw_payloads", raw_glob.resolve().as_posix())
    else:
        _create_empty_view(connection, "raw_payloads", _EMPTY_RAW_PAYLOADS)


def connect_lens(settings: ArchiveSettings) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with views over the configured archive.

    The connection attaches no database and creates no archive write path.  S3 credentials
    live only in DuckDB's temporary in-memory secret manager for this connection's lifetime.
    """
    duckdb_module = _require_duckdb()
    connection = duckdb_module.connect(database=":memory:")
    try:
        if settings.archive_backend == "local":
            _configure_local_views(connection, Path(settings.archive_local_dir))
        elif settings.archive_backend == "s3":
            root = _configure_s3(connection, settings)
            _configure_remote_view(
                connection,
                "bars",
                f"{root}/price_bars/bar_size=*/*.parquet",
                _EMPTY_BARS,
            )
            _configure_remote_view(
                connection,
                "raw_payloads",
                f"{root}/raw/*/*.parquet",
                _EMPTY_RAW_PAYLOADS,
            )
        else:
            raise RuntimeError(
                "no archive backend configured — set ARCHIVE_BACKEND=s3 or "
                "ARCHIVE_BACKEND=local in .env"
            )
    except Exception:
        connection.close()
        raise
    return connection


__all__ = ["connect_lens"]
