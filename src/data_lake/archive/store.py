"""Object-store backends for the archive: a local directory or any S3-compatible bucket.

``S3ObjectStore`` takes an already-built client so tests (and callers with exotic setups)
can inject a fake; ``from_settings`` builds a real boto3 client from the caller's settings — the
boto3 import lives behind the ``[archive]`` extra. Credentials reach this package through
the settings object only. Keys are POSIX-style relative paths (``price_bars/…/2025-06.parquet``).
"""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from data_lake.runtime import resolve_settings
from data_lake.settings import ArchiveSettings

logger = logging.getLogger(__name__)

#: Whole-object GETs tried before a broken download is raised. botocore's own retries cover
#: the request up to the response headers, never the body read that follows them.
DEFAULT_READ_ATTEMPTS = 3
#: Seconds before the first re-fetch; doubled per further attempt.
READ_RETRY_DELAY = 2.0


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int


class ObjectStore(Protocol):
    """Minimal blob interface: whole-object put/get plus listing."""

    def put_bytes(self, key: str, data: bytes) -> None: ...

    def get_bytes(self, key: str) -> bytes:
        """Raises KeyError if the object does not exist."""
        ...

    def exists(self, key: str) -> bool: ...

    def list_objects(self, prefix: str = "") -> list[StoredObject]: ...


def _is_missing_s3_object(exc: Exception) -> bool:
    """Return whether a botocore-style client error represents a missing object."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    code = error.get("Code") if isinstance(error, dict) else None
    return str(code) in {"404", "NoSuchKey", "NotFound"}


def _broken_download_errors() -> tuple[type[Exception], ...]:
    """What botocore's ``StreamingBody.read`` raises when a download dies part-way.

    The connection dropping (``ResponseStreamingError``), the body ending short of its
    ``Content-Length`` (``IncompleteReadError``) and the socket stalling past the read
    timeout (``ReadTimeoutError``). Imported lazily: botocore comes with the ``archive``
    extra, and without it no real S3 client exists to raise them.
    """
    try:
        from botocore.exceptions import (
            IncompleteReadError,
            ReadTimeoutError,
            ResponseStreamingError,
        )
    except ImportError:  # pragma: no cover - exercised only without the extra
        return ()
    return (ResponseStreamingError, IncompleteReadError, ReadTimeoutError)


class LocalDirStore:
    """Archive to a plain directory (external drive, NAS mount) — also the test backend."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key {key!r} escapes the archive root")
        return path

    def put_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # write-then-rename so a crash mid-write never leaves a truncated object behind
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        if not self.root.is_dir():
            return []
        objects = [
            StoredObject(key=path.relative_to(self.root).as_posix(), size=path.stat().st_size)
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.endswith(".tmp")
        ]
        return sorted(
            (obj for obj in objects if obj.key.startswith(prefix)), key=lambda obj: obj.key
        )


class S3ObjectStore:
    """S3-compatible bucket (Cloudflare R2, Backblaze B2, MinIO, AWS).

    ``get_bytes`` re-fetches an object whose download breaks mid-body, up to
    ``read_attempts`` GETs in all. A whole-object GET is idempotent, and without this one
    dropped connection on one multi-megabyte partition failed the whole social-posts load.
    """

    def __init__(
        self,
        client,
        bucket: str,
        *,
        prefix: str = "",
        read_attempts: int = DEFAULT_READ_ATTEMPTS,
    ):
        if not bucket:
            raise ValueError("S3 archive needs a bucket name (ARCHIVE_S3_BUCKET)")
        if read_attempts < 1:
            raise ValueError(f"read_attempts must be at least 1, got {read_attempts}")
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.read_attempts = read_attempts

    @classmethod
    def from_settings(cls, settings: ArchiveSettings) -> "S3ObjectStore":
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise RuntimeError(
                "the s3 archive backend needs the archive extra — "
                "install with: uv sync --extra archive"
            ) from exc
        client = boto3.client(
            "s3",
            endpoint_url=settings.archive_s3_endpoint_url or None,
            region_name=settings.archive_s3_region or None,
            aws_access_key_id=settings.archive_s3_access_key_id or None,
            aws_secret_access_key=settings.archive_s3_secret_access_key or None,
        )
        return cls(client, settings.archive_s3_bucket, prefix=settings.archive_s3_prefix)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put_bytes(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get_bytes(self, key: str) -> bytes:
        broken = _broken_download_errors()
        for attempt in range(1, self.read_attempts + 1):
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
            except Exception as exc:
                if _is_missing_s3_object(exc):
                    raise KeyError(key) from None
                raise
            try:
                return response["Body"].read()
            except broken as exc:
                if attempt == self.read_attempts:
                    raise
                delay = READ_RETRY_DELAY * 2 ** (attempt - 1)
                logger.warning(
                    "archive read of %s broke on attempt %d/%d (%s); re-fetching in %.0fs",
                    key,
                    attempt,
                    self.read_attempts,
                    type(exc).__name__,
                    delay,
                )
                time.sleep(delay)
        raise AssertionError("unreachable: the last attempt returns or raises")  # pragma: no cover

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except Exception as exc:
            if _is_missing_s3_object(exc):
                return False
            raise
        return True

    def list_objects(self, prefix: str = "") -> list[StoredObject]:
        objects: list[StoredObject] = []
        strip = len(f"{self.prefix}/") if self.prefix else 0
        kwargs = {"Bucket": self.bucket, "Prefix": self._key(prefix)}
        while True:
            page = self.client.list_objects_v2(**kwargs)
            objects += [
                StoredObject(key=item["Key"][strip:], size=item["Size"])
                for item in page.get("Contents", [])
            ]
            if not page.get("IsTruncated"):
                return sorted(objects, key=lambda obj: obj.key)
            kwargs["ContinuationToken"] = page["NextContinuationToken"]


def store_from_settings(settings: ArchiveSettings | None = None) -> ObjectStore:
    """The configured archive backend; refuses to guess when none is configured.

    Takes the narrow :class:`ArchiveSettings` so a caller that only archives need not hold
    provider credentials, while the process-wide fallback is a full ``LakeSettings``.
    """
    resolved: ArchiveSettings = settings if settings is not None else resolve_settings(None)
    if resolved.archive_backend == "local":
        return LocalDirStore(resolved.archive_local_dir)
    if resolved.archive_backend == "s3":
        return S3ObjectStore.from_settings(resolved)
    raise RuntimeError(
        "no archive backend configured — set the archive backend to 's3' (Cloudflare R2 / "
        "Backblaze B2) or 'local' in the settings object handed to data_lake.configure()"
    )
