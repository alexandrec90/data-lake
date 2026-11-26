"""What both end-to-end tiers read a SeaweedFS pool through.

`tests/test_e2e_seaweedfs.py` (`ci_e2e`) starts a one-node pool of its own on the runner;
`tests/test_e2e_pool.py` (`local`) reaches the real one on the desktops. Either way the
tests see a `Pool` and ask the same master and filer the same questions.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from data_lake.archive.store import S3ObjectStore, store_from_settings
from data_lake.testing import FakeLakeSettings
from test_project_scripts import load_script

sn = load_script("storage-node.py")

BUCKET = "data-lake"


@dataclass(frozen=True)
class Pool:
    """What the pool looks like from this machine, right now."""

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


def store(settings: FakeLakeSettings) -> S3ObjectStore:
    found = store_from_settings(settings)
    assert isinstance(found, S3ObjectStore)
    return found


def holders(pool: Pool, key: str) -> list[set[str]]:
    """For each chunk of `key`, the PCs a copy of it is on, as the filer and master say."""
    path = urllib.parse.quote(f"/buckets/{BUCKET}/{key}", safe="/=")
    url = f"http://{pool.primary}:{sn.FILER_PORT}{path}?metadata=true"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the pool's filer
        chunks = json.load(response)["chunks"]
    assert chunks, f"the filer lists no chunks for {key}"
    found = []
    for chunk in chunks:
        volume = chunk["file_id"].partition(",")[0]
        master = f"http://{pool.primary}:{sn.MASTER_PORT}"
        located = sn._get_json(f"{master}/dir/lookup?volumeId={volume}")
        found.append({entry["url"].rpartition(":")[0] for entry in located["locations"]})
    return found


def assert_copies(pool: Pool, key: str) -> None:
    wanted = min(pool.copies, len(pool.hosts))
    for chunk_holders in holders(pool, key):
        assert len(chunk_holders) >= wanted, f"{key} is on {chunk_holders}, wanted {wanted} PCs"
