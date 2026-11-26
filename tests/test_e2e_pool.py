"""The archive as it is on the real SeaweedFS pool: the tier `scripts/e2e.py` runs.

What SeaweedFS itself does -- a missing key, the master's JSON, a round trip through the
gateway, DuckDB's httpfs -- is `tests/test_e2e_seaweedfs.py`, against a pool of its own
on a runner. What is left here needs **this PC**: the pool's keys, which only a pool PC
or a consumer checkout beside it holds, and the user's own archive, which is the thing
under test. Both tests only read it, so nothing here writes to the shared bucket.

**A desktop being off is not a red.** Reads keep working with a PC away, and the copies
asserted are capped at the PCs the master can see, since a fixer cannot switch one on.

Credentials come from this PC's pool home, or from a sibling `.env` on a volume node, and
never reach a test's repr: see `Pool.settings`.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from _db import make_session_scope
from _pool import BUCKET, Pool, assert_copies, sn, store
from data_lake.archive.catalog import list_datasets, load_manifest
from data_lake.archive.parquet_io import parquet_bytes_to_frame
from data_lake.db.models import SocialPost
from data_lake.ingestion.social.social_scraper import (
    DATASET,
    SocialScraperConnector,
    partition_platform,
)
from data_lake.testing import lake_settings

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
pytest.importorskip("botocore.exceptions")

pytestmark = pytest.mark.local

NOW = datetime(2026, 7, 1, tzinfo=UTC)


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


def test_every_catalogued_dataset_reads_back_from_two_pcs(pool):
    """The shared bucket as consumers find it: each manifest parses, its smallest partition
    decodes, and that partition is held by as many PCs as the placement promises. Read
    through `load_manifest`, since `load_catalog` skips an unreadable one on purpose."""
    found = store(pool.settings)
    names = list_datasets(found)
    assert DATASET in names, f"no {DATASET} manifest: consumers would get no social data"
    for name in names:
        manifest = load_manifest(found, name)
        assert manifest is not None, name
        if not manifest.partitions:
            continue
        key, entry = min(manifest.partitions.items(), key=lambda item: item[1].rows)
        frame = parquet_bytes_to_frame(found.get_bytes(key))
        # At least: an export merging into this partition may land after the manifest read.
        assert len(frame) >= entry.rows, key
        assert_copies(pool, key)


def test_the_social_export_loads_into_a_consumers_table(pool):
    """`social_posts` is only ever filled this way, from a layout social-scraper owns.

    The dataset holds tens of millions of rows, so a seeded watermark narrows the run to
    the last export's partitions of whichever platform has the fewest rows in it. The
    selection is the connector's own, and the seeded row is the only one it starts with.
    """
    found = store(pool.settings)
    manifest = load_manifest(found, DATASET)
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

    connector = SocialScraperConnector(pool.settings, scope, store=found)
    loaded = connector.fetch(platforms=[platform], lookback=timedelta(0))

    assert loaded > 0
    with scope() as session:
        hashes = session.execute(
            select(SocialPost.author_hash).where(SocialPost.channel != "e2e")
        ).scalars()
        # sha256 hex: CLAUDE.md's "people are stored hashed only", at the real boundary.
        assert all(h is None or len(h) == 64 for h in hashes)
