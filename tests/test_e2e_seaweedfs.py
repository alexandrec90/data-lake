"""The archive against a real SeaweedFS: the tier `scripts/e2e.py --ci` runs.

Everything else under `tests/` fakes the S3 client, so nothing there can say whether
SeaweedFS answers a missing key the way `S3ObjectStore` expects, whether DuckDB's httpfs
can read it at all, or whether the master's JSON still parses. CLAUDE.md's
"Verify-then-delete deletes for real" is why that matters: past the Postgres window the
archive is the only copy.

None of that needs the desktops, so this starts a one-node pool of its own: the `weed`
release `scripts/storage-node.py` pins (`VERSION`), run with that script's own primary
command line (`weed_args`), on loopback, in a temp directory, with keys minted for the
run. The `E2E` job in `.github/workflows/pr-gate.yml` downloads that release and names it
in `$WEED`. What does need the real pool -- its keys and the user's archive -- stays in
`tests/test_e2e_pool.py`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from _db import make_session_scope
from _pool import BUCKET, Pool, assert_copies, sn, store
from data_lake.archive.bars import archive_price_bars, restore_price_bars
from data_lake.archive.catalog import load_manifest
from data_lake.archive.lens import connect_lens
from data_lake.db.models import Instrument, PriceBar
from data_lake.testing import FakeLakeSettings, lake_settings

pytest.importorskip("pyarrow")
pytest.importorskip("duckdb")
client_errors = pytest.importorskip("botocore.exceptions")

pytestmark = pytest.mark.ci_e2e

# The `weed` binary to run; else `weed` on PATH.
WEED_ENV = "WEED"
HOST = "127.0.0.1"
# A first start formats the filer's store and grows the bucket's first volumes.
READY_SECONDS = 120
NOW = datetime(2026, 7, 1, tzinfo=UTC)
JANUARY = datetime(2024, 1, 15, 14, 30, tzinfo=UTC)
FEBRUARY = datetime(2024, 2, 1, 15, 0, tzinfo=UTC)
CLOSES = {JANUARY: 11.0, FEBRUARY: 13.0}

# The desktop's sizing, which a runner's disk cannot give: 100 GiB kept free and 8 GB
# volumes would leave it no writable volume at all. Everything else is the pool's own.
RUNNER_SIZING = {
    "-volume.minFreeSpace": "1GiB",
    "-master.volumeSizeLimitMB": "64",
    "-volume.max": "16",
}


def runner_args(args: Sequence[str], sizing: dict[str, str] = RUNNER_SIZING) -> list[str]:
    """`args` with each `sizing` flag's value replaced; a flag gone from `args` fails."""
    out = list(args)
    for flag, value in sizing.items():
        at = [i for i, arg in enumerate(out) if arg.partition("=")[0] == flag]
        assert at, f"storage-node.py's weed_args no longer passes {flag}"
        out[at[0]] = f"{flag}={value}"
    return out


def _weed() -> str:
    found = os.environ.get(WEED_ENV) or shutil.which("weed")
    if not found:
        pytest.fail(f"no weed binary: set {WEED_ENV} or put weed {sn.VERSION} on PATH")
    return found


def _wait_for_volume_server(deadline: float) -> dict:
    status = f"http://{HOST}:{sn.MASTER_PORT}/dir/status"
    while True:
        try:
            topology = sn._get_json(status)
            if sn.topology_hosts(topology):
                return topology
        except (OSError, urllib.error.URLError, ValueError):
            pass
        if time.monotonic() > deadline:
            pytest.fail(f"weed never listed a volume server at {status}")
        time.sleep(1)


def _create_bucket(settings: FakeLakeSettings, deadline: float) -> None:
    """Through the gateway, as a consumer would see it; it answers after the master."""
    client = store(settings).client
    while True:
        try:
            client.create_bucket(Bucket=BUCKET)
            return
        except (client_errors.BotoCoreError, client_errors.ClientError) as error:
            if time.monotonic() > deadline:
                pytest.fail(f"the S3 gateway never created {BUCKET}: {error}")
            time.sleep(1)


@pytest.fixture(scope="module")
def pool(tmp_path_factory) -> Iterator[Pool]:
    """A one-node pool, started for this module and stopped after it."""
    weed = _weed()
    home: Path = tmp_path_factory.mktemp("seaweedfs")
    for name in ("data", "logs"):
        (home / name).mkdir()
    access, secret = sn.new_credentials()
    identities = json.dumps(sn.s3_identities(access, secret))
    (home / "s3.json").write_text(identities, encoding="utf-8")
    node = sn.Node(role="primary", ip=HOST, primary=HOST, replication="000")
    argv = [weed, *runner_args(sn.weed_args(node, home))]
    settings = lake_settings(
        archive_backend="s3",
        archive_s3_bucket=BUCKET,
        archive_s3_endpoint_url=sn.pool_url(HOST),
        archive_s3_region="us-east-1",
        archive_s3_access_key_id=access,
        archive_s3_secret_access_key=secret,
    )
    placement = sn.bucket_replication(sn.BUCKETS, node.replication)[BUCKET]
    # weed's own output, beside its `-logdir`: where a fixer looks when it never came up.
    with (home / "weed.out").open("wb") as log:
        process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + READY_SECONDS
            topology = _wait_for_volume_server(deadline)
            _create_bucket(settings, deadline)
            yield Pool(
                settings=settings,
                primary=HOST,
                hosts=tuple(sorted(sn.topology_hosts(topology))),
                copies=1 + sum(int(digit) for digit in placement),
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.fixture
def scratch(pool: Pool):
    """Settings and a store confined to a prefix of this run's own, emptied afterwards."""
    settings = replace(pool.settings, archive_s3_prefix=f"_e2e/{uuid4().hex}")
    found = store(settings)
    yield settings, found
    for obj in found.list_objects():
        found.client.delete_object(Bucket=found.bucket, Key=f"{found.prefix}/{obj.key}")
    assert found.list_objects() == []


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


def test_runner_args_replaces_only_the_sizing():
    args = ["server", "-volume.max=0", "-volume.minFreeSpace=100GiB", "-s3"]
    sizing = {"-volume.max": "16", "-volume.minFreeSpace": "1GiB"}
    expected = ["server", "-volume.max=16", "-volume.minFreeSpace=1GiB", "-s3"]
    assert runner_args(args, sizing) == expected


def test_runner_args_fails_on_a_flag_the_pool_stopped_passing():
    with pytest.raises(AssertionError, match="-volume.max"):
        runner_args(["server"], {"-volume.max": "16"})


def test_the_master_lists_the_primary_among_its_volume_servers(pool):
    """Pins the `/dir/status` shape `storage-node.py status` and `pc_away` both parse: a
    weed upgrade that moved it would read as every PC being off."""
    assert pool.primary in pool.hosts


def test_a_missing_key_reads_as_missing(scratch):
    """`load_manifest` tells "never catalogued" from damage on exactly this, and only a
    real gateway can say which error code it sends."""
    _, found = scratch
    assert found.exists("no/such/object.parquet") is False
    with pytest.raises(KeyError):
        found.get_bytes("no/such/object.parquet")


def test_bars_round_trip_through_the_pool(pool, scratch):
    """Rows to Parquet to the pool, verified, catalogued, read by the lens, then restored
    into a rebuilt database."""
    settings, found = scratch
    scope = make_session_scope()
    with scope() as session:
        _seed_bars(session)

    with scope() as session:
        result = archive_price_bars(session, found, older_than_days=365, now=NOW)
    assert result.rows_archived == result.rows_removed == len(CLOSES)
    with scope() as session:
        assert _closes(session) == []

    for key in result.objects:
        assert_copies(pool, f"{found.prefix}/{key}")

    manifest = load_manifest(found, "price_bars")
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
        restored = restore_price_bars(session, found, start=date(2024, 1, 1), end=date(2024, 2, 29))
    assert restored == len(CLOSES)
    with rebuilt() as session:
        assert _closes(session) == sorted(CLOSES.values())
