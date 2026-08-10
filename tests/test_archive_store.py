"""Object-store backends: LocalDirStore round-trips, S3ObjectStore against a fake client
(never a real bucket), and backend dispatch from the settings object."""

import pytest

from data_lake.archive.store import (
    LocalDirStore,
    S3ObjectStore,
    StoredObject,
    store_from_settings,
)
from data_lake.testing import lake_settings

# --- LocalDirStore --------------------------------------------------------------------


def test_local_store_roundtrip_and_listing(tmp_path):
    store = LocalDirStore(tmp_path)
    store.put_bytes("price_bars/bar_size=1_min/2024-01.parquet", b"one")
    store.put_bytes("raw/news_articles/2024-02.parquet", b"three")

    assert store.get_bytes("price_bars/bar_size=1_min/2024-01.parquet") == b"one"
    assert store.exists("raw/news_articles/2024-02.parquet")
    assert not store.exists("nope.parquet")
    assert store.list_objects() == [
        StoredObject("price_bars/bar_size=1_min/2024-01.parquet", 3),
        StoredObject("raw/news_articles/2024-02.parquet", 5),
    ]
    assert store.list_objects("raw/") == [StoredObject("raw/news_articles/2024-02.parquet", 5)]


def test_local_store_overwrite_replaces_content(tmp_path):
    store = LocalDirStore(tmp_path)
    store.put_bytes("a.parquet", b"old")
    store.put_bytes("a.parquet", b"newer")
    assert store.get_bytes("a.parquet") == b"newer"


def test_local_store_missing_key_raises_keyerror(tmp_path):
    with pytest.raises(KeyError):
        LocalDirStore(tmp_path).get_bytes("missing.parquet")


def test_local_store_rejects_escaping_keys(tmp_path):
    store = LocalDirStore(tmp_path / "root")
    with pytest.raises(ValueError, match="escapes"):
        store.put_bytes("../outside.parquet", b"x")


def test_local_store_empty_root_lists_nothing(tmp_path):
    assert LocalDirStore(tmp_path / "never-created").list_objects() == []


# --- S3ObjectStore (fake client) ------------------------------------------------------


class _S3ClientError(Exception):
    """Minimal botocore ClientError shape without depending on the optional package."""

    def __init__(self, code: str, key: str):
        super().__init__(f"{code}: {key}")
        self.response = {"Error": {"Code": code}}


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Client:
    """Just enough of boto3's S3 client for S3ObjectStore, incl. truncated listing."""

    def __init__(self, page_size: int = 2):
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.page_size = page_size

    def put_object(self, Bucket, Key, Body):
        self.blobs[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.blobs:
            raise _S3ClientError("NoSuchKey", Key)
        return {"Body": _FakeBody(self.blobs[(Bucket, Key)])}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.blobs:
            raise _S3ClientError("404", Key)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(
            key for bucket, key in self.blobs if bucket == Bucket and key.startswith(Prefix)
        )
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        response = {
            "Contents": [{"Key": key, "Size": len(self.blobs[(Bucket, key)])} for key in page],
            "IsTruncated": truncated,
        }
        if truncated:
            response["NextContinuationToken"] = str(start + self.page_size)
        return response


def test_s3_store_roundtrip_with_prefix():
    client = FakeS3Client()
    store = S3ObjectStore(client, "bucket", prefix="ibkr")
    store.put_bytes("price_bars/x.parquet", b"data")

    assert client.blobs[("bucket", "ibkr/price_bars/x.parquet")] == b"data"  # prefix applied
    assert store.get_bytes("price_bars/x.parquet") == b"data"
    assert store.exists("price_bars/x.parquet")
    assert not store.exists("absent.parquet")


def test_s3_store_missing_key_raises_keyerror():
    with pytest.raises(KeyError):
        S3ObjectStore(FakeS3Client(), "bucket").get_bytes("missing.parquet")


def test_s3_store_does_not_hide_non_missing_client_errors():
    class AccessDeniedClient(FakeS3Client):
        def get_object(self, Bucket, Key):
            raise _S3ClientError("AccessDenied", Key)

        def head_object(self, Bucket, Key):
            raise _S3ClientError("AccessDenied", Key)

    store = S3ObjectStore(AccessDeniedClient(), "bucket")
    with pytest.raises(_S3ClientError, match="AccessDenied"):
        store.get_bytes("private.parquet")
    with pytest.raises(_S3ClientError, match="AccessDenied"):
        store.exists("private.parquet")


def test_s3_store_listing_paginates_and_strips_prefix():
    store = S3ObjectStore(FakeS3Client(page_size=2), "bucket", prefix="pre")
    for name in ("a", "b", "c", "d", "e"):
        store.put_bytes(f"{name}.parquet", name.encode())
    assert store.list_objects() == [
        StoredObject(f"{name}.parquet", 1) for name in ("a", "b", "c", "d", "e")
    ]
    assert store.list_objects("a") == [StoredObject("a.parquet", 1)]


def test_s3_store_requires_bucket():
    with pytest.raises(ValueError, match="bucket"):
        S3ObjectStore(FakeS3Client(), "")


# --- store_from_settings --------------------------------------------------------------


def _settings(**overrides) -> object:
    return lake_settings(**overrides)


def test_store_from_settings_none_backend_refuses():
    with pytest.raises(RuntimeError, match="no archive backend configured"):
        store_from_settings(_settings())


def test_store_from_settings_local_backend(tmp_path):
    store = store_from_settings(_settings(archive_backend="local", archive_local_dir=str(tmp_path)))
    assert isinstance(store, LocalDirStore)
    assert store.root == tmp_path


def test_store_from_settings_s3_backend_builds_client():
    pytest.importorskip("boto3")
    store = store_from_settings(
        _settings(
            archive_backend="s3",
            archive_s3_bucket="my-bucket",
            archive_s3_endpoint_url="https://example.r2.cloudflarestorage.com",
            archive_s3_access_key_id="key",
            archive_s3_secret_access_key="secret",  # pragma: allowlist secret - fixture, no bucket exists
        )
    )
    assert isinstance(store, S3ObjectStore)
    assert store.bucket == "my-bucket"


def test_store_from_settings_s3_backend_requires_bucket():
    pytest.importorskip("boto3")
    with pytest.raises(ValueError, match="bucket"):
        store_from_settings(_settings(archive_backend="s3"))
