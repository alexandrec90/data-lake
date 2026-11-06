"""The archive against the real SeaweedFS pool: the tier `scripts/e2e.py` runs.

Everything else under `tests/` fakes the S3 client, so nothing there can say whether
SeaweedFS answers a missing key the way `S3ObjectStore` expects, whether DuckDB's httpfs
can read the pool at all, whether the master's JSON still parses, or whether the archive
really is on two PCs. CLAUDE.md's "Verify-then-delete deletes for real" is why that
matters: past the Postgres window the archive is the only copy.

**What is written stays out of the way.** Writes land under `_e2e/<run>/`, a prefix no
dataset uses, with the catalog beneath it too, and are deleted afterwards. Everything else
here only reads the shared archive.

**A desktop being off is not a red.** With two storage PCs a `001` write fails while one
is away, which is the documented cost and not a defect, and a fixer cannot switch a PC on.
So the round trip takes the shape the pool is in: with both PCs up it runs in full, and
with one away it asserts the failure is loud and leaves the rows where they were.

Credentials come from this PC's pool home, or from a sibling `.env` on a volume node, and
never reach a test's repr: see `Pool.settings`.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from _db import make_session_scope
from data_lake.archive.bars import archive_price_bars, restore_price_bars
from data_lake.archive.catalog import list_datasets, load_manifest
from data_lake.archive.lens import connect_lens
from data_lake.archive.parquet_io import parquet_bytes_to_frame
from data_lake.archive.store import S3ObjectStore, store_from_settings
from data_lake.db.models import Instrument, PriceBar, SocialPost
from data_lake.ingestion.social.social_scraper import (
    DATASET,
    SocialScraperConnector,
    partition_platform,
)
from data_lake.testing import FakeLakeSettings, lake_settings
from test_project_scripts import load_script

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
client_errors = pytest.importorskip("botocore.exceptions")

pytestmark = pytest.mark.local

sn = load_script("storage-node.py")

BUCKET = "data-lake"
NOW = datetime(2026, 7, 1, tzinfo=UTC)
JANUARY = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)
FEBRUARY = datetime(2024, 2, 1, 15, 0, tzinfo=UTC)
CLOSES = {JANUARY: 11.0, FEBRUARY: 13.0}


@dataclass(frozen=True)
class Pool:
    """What the pool looks like from this PC, right now."""

    # Holds the secret key, and pytest prints a failing fixture value's repr.
    settings: FakeLakeSettings = field(repr=False)
    primary: str
    # The volume servers the master can see, which is every PC that is on.
    hosts: tuple[str, ...]
    # How many PCs the archive bucket's placement wants a copy on.
    copies: int

    @property
    def pc_away(self) -> bool:
        return len(self.hosts) < self.copies


def _checkouts_root() -> Path:
    """The directory of sibling checkouts, from this checkout or a worktree of it."""
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=Path(__file__).parent,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(common.stdout.strip()).parent.parent


def _pool_keys(home: Path) -> tuple[str, str] | None:
    """The gateway's own keys on the primary, else those a consumer's `.env` holds."""
    s3 = home / "s3.json"
    if s3.is_file():
        identity = json.loads(s3.read_text(encoding="utf-8"))["identities"][0]
        credential = identity["credentials"][0]
        return credential["accessKey"], credential["secretKey"]
    for env in sorted(_checkouts_root().glob("*/.env")):
        keys = sn.pool_keys(env.read_text(encoding="utf-8", errors="replace"))
        if keys:
            return keys
    return None


@pytest.fixture(scope="module")
def pool() -> Pool:
    """Failed, not skipped, off the pool: every test here needs it, so such a run has
    executed nothing and is red either way, and this says why."""
    home = sn.HOME
    if not (home / "node.json").is_file():
        pytest.fail(f"this PC is not in the pool: no node.json in {home}")
    keys = _pool_keys(home)
    if keys is None:
        pytest.fail(f"no pool keys: no s3.json in {home} and no consumer .env holding them")
    node = sn._read_node(home)
    topology = sn._get_json(f"http://{node.primary}:{sn.MASTER_PORT}/dir/status")
    placement = sn.bucket_replication(sn.BUCKETS, node.replication)[BUCKET]
    return Pool(
        settings=lake_settings(
            archive_backend="s3",
            archive_s3_bucket=BUCKET,
            archive_s3_endpoint_url=sn.pool_url(node.primary),
            archive_s3_region="us-east-1",
            archive_s3_access_key_id=keys[0],
            archive_s3_secret_access_key=keys[1],
        ),
        primary=node.primary,
        hosts=tuple(sorted(sn.topology_hosts(topology))),
        copies=1 + sum(int(digit) for digit in placement),
    )


def _store(settings: FakeLakeSettings) -> S3ObjectStore:
    store = store_from_settings(settings)
    assert isinstance(store, S3ObjectStore)
    return store


@pytest.fixture
def scratch(pool: Pool):
    """Settings and a store confined to a prefix of this run's own, emptied afterwards."""
    settings = replace(pool.settings, archive_s3_prefix=f"_e2e/{uuid4().hex}")
    store = _store(settings)
    yield settings, store
    for obj in store.list_objects():
        store.client.delete_object(Bucket=store.bucket, Key=f"{store.prefix}/{obj.key}")
    assert store.list_objects() == []


def _holders(pool: Pool, key: str) -> list[set[str]]:
    """For each chunk of `key`, the PCs a copy of it is on, as the filer and master say."""
    path = urllib.parse.quote(f"/buckets/{BUCKET}/{key}", safe="/=")
    url = f"http://{pool.primary}:{sn.FILER_PORT}{path}?metadata=true"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - tailnet node
        chunks = json.load(response)["chunks"]
    assert chunks, f"the filer lists no chunks for {key}"
    holders = []
    for chunk in chunks:
        volume = chunk["file_id"].partition(",")[0]
        master = f"http://{pool.primary}:{sn.MASTER_PORT}"
        found = sn._get_json(f"{master}/dir/lookup?volumeId={volume}")
        holders.append({entry["url"].rpartition(":")[0] for entry in found["locations"]})
    return holders


def _assert_copies(pool: Pool, key: str) -> None:
    wanted = min(pool.copies, len(pool.hosts))
    for chunk_holders in _holders(pool, key):
        assert len(chunk_holders) >= wanted, f"{key} is on {chunk_holders}, wanted {wanted} PCs"


def _seed_bars(session) -> None:
    session.add(Instrument(id=1, symbol="AAPL", exchange="SMART", currency="USD"))
    session.add_all(
        PriceBar(
            instrument_id=1,
            ts=ts,
            bar_size="1 min",
            source="e2e",
            what_to_show="TRADES",
            open=close - 1,
            high=close + 1,
            low=close - 2,
            close=close,
            volume=100.0,
        )
        for ts, close in CLOSES.items()
    )


def _closes(session) -> list[float]:
    return sorted(session.execute(select(PriceBar.close)).scalars())


def test_the_master_lists_the_primary_among_its_volume_servers(pool):
    """Pins the `/dir/status` shape `storage-node.py status` and `pc_away` both parse: a
    weed upgrade that moved it would read as every PC being off."""
    assert pool.primary in pool.hosts


def test_a_missing_key_reads_as_missing(scratch):
    """`load_manifest` tells "never catalogued" from damage on exactly this, and only a
    real gateway can say which error code it sends."""
    _, store = scratch
    assert store.exists("no/such/object.parquet") is False
    with pytest.raises(KeyError):
        store.get_bytes("no/such/object.parquet")


def test_bars_round_trip_through_the_pool_or_stay_put(pool, scratch):
    """Rows to Parquet to the pool, verified, catalogued, read by the lens, then restored
    into a rebuilt database. With a PC away the write must fail and keep the rows."""
    settings, store = scratch
    scope = make_session_scope()
    with scope() as session:
        _seed_bars(session)

    if pool.pc_away:
        with pytest.raises(client_errors.ClientError), scope() as session:
            archive_price_bars(session, store, older_than_days=365, now=NOW)
        with scope() as session:
            assert _closes(session) == sorted(CLOSES.values())
        return

    with scope() as session:
        result = archive_price_bars(session, store, older_than_days=365, now=NOW)
    assert result.rows_archived == result.rows_removed == len(CLOSES)
    with scope() as session:
        assert _closes(session) == []

    for key in result.objects:
        _assert_copies(pool, f"{store.prefix}/{key}")

    manifest = load_manifest(store, "price_bars")
    assert manifest is not None
    assert set(manifest.partitions) == set(result.objects)
    assert manifest.total_rows == len(CLOSES)

    lens = connect_lens(settings)
    try:
        assert lens.execute("SELECT count(*), sum(close) FROM bars").fetchone() == (
            len(CLOSES),
            sum(CLOSES.values()),
        )
    finally:
        lens.close()

    rebuilt = make_session_scope()
    with rebuilt() as session:
        restored = restore_price_bars(session, store, start=date(2024, 1, 1), end=date(2024, 2, 29))
    assert restored == len(CLOSES)
    with rebuilt() as session:
        assert _closes(session) == sorted(CLOSES.values())


def test_every_catalogued_dataset_reads_back_from_two_pcs(pool):
    """The shared bucket as consumers find it: each manifest parses, its smallest partition
    decodes, and that partition is held by as many PCs as the placement promises. Read
    through `load_manifest`, since `load_catalog` skips an unreadable one on purpose."""
    store = _store(pool.settings)
    names = list_datasets(store)
    assert DATASET in names, f"no {DATASET} manifest: consumers would get no social data"
    for name in names:
        manifest = load_manifest(store, name)
        assert manifest is not None, name
        if not manifest.partitions:
            continue
        key, entry = min(manifest.partitions.items(), key=lambda item: item[1].rows)
        frame = parquet_bytes_to_frame(store.get_bytes(key))
        # At least: an export merging into this partition may land after the manifest read.
        assert len(frame) >= entry.rows, key
        _assert_copies(pool, key)


def test_the_social_export_loads_into_a_consumers_table(pool):
    """`social_posts` is only ever filled this way, from a layout social-scraper owns.

    The dataset holds tens of millions of rows, so a seeded watermark narrows the run to
    the last export's partitions of whichever platform has the fewest rows in it. The
    selection is the connector's own, and the seeded row is the only one it starts with.
    """
    store = _store(pool.settings)
    manifest = load_manifest(store, DATASET)
    assert manifest is not None

    latest: dict[str, datetime] = {}
    for key, entry in manifest.partitions.items():
        platform = partition_platform(key)
        if platform is not None:
            latest[platform] = max(entry.updated_at, latest.get(platform, entry.updated_at))
    in_last_export: Counter[str] = Counter()
    for key, entry in manifest.partitions.items():
        platform = partition_platform(key)
        if platform is not None and entry.updated_at == latest[platform]:
            in_last_export[platform] += entry.rows
    platform = min(in_last_export, key=lambda name: in_last_export[name])

    scope = make_session_scope()
    with scope() as session:
        session.add(
            SocialPost(
                platform=platform,
                channel="e2e",
                external_id="e2e-watermark",
                created_at=NOW,
                fetched_at=latest[platform],
            )
        )

    connector = SocialScraperConnector(pool.settings, scope, store=store)
    loaded = connector.fetch(platforms=[platform], lookback=timedelta(0))

    assert loaded > 0
    with scope() as session:
        hashes = session.execute(
            select(SocialPost.author_hash).where(SocialPost.channel != "e2e")
        ).scalars()
        # sha256 hex: CLAUDE.md's "people are stored hashed only", at the real boundary.
        assert all(h is None or len(h) == 64 for h in hashes)
