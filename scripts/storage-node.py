#!/usr/bin/env python3
"""Join this PC to the home SeaweedFS pool that backs the data-lake archive.

The archive's `s3` backend talks to any S3-compatible store, so pooling the disks of
several PCs needs no change to the package: SeaweedFS runs on each one, and consumers
point `ARCHIVE_S3_ENDPOINT_URL` at the pool. One PC is the **primary** -- it runs the
master, the filer (the name -> chunk index) and the S3 gateway, plus a volume server.
Every other PC is a **volume** node: a volume server that stores chunks and registers
with the primary. Adding a PC later is `install --role volume` on it and nothing else.

Everything listens on the Tailscale address only, so the pool is reachable from the
tailnet and from nowhere else -- not the LAN, not the WARP tunnel.

Two buckets, two placements, set once on the primary with `configure-buckets`:

- `data-lake` -- the archive. `001`: a second copy on another server, because past the
  Postgres window the archive is the only copy there is.
- `raw-dumps` -- downloaded source dumps (StockTwits CSVs, Reddit `.zst`). `000`: one
  copy, because they can be downloaded again and doubling them halves the pool.

With only two storage PCs, a `001` write needs both up: while one is off the archive
reads but does not accept new files. That was the accepted trade for not running a third
coordinator.

A bucket can be pinned to one node (`configure-buckets --pin raw-dumps=IP`), so the dumps
that fill a small drive land on a big one. The pin is kept in `pins.json` beside
`node.json`, so a later `configure-buckets` re-applies it rather than dropping it. Pin a
bucket before it has volumes elsewhere: the master hands out an existing writable volume
before growing one on the pinned node, so until those are deleted every write fails.

The filer's index -- which chunks make up which key -- exists on the primary only, so
every node's supervisor saves a copy of it daily to its own `meta/`, the volume nodes over
the tailnet. If the primary's drive dies, a volume node still holds the bytes (`001`) and
the index: a new primary restores it with `fs.meta.load` in `weed shell`.

Losing or retiring the primary PC
---------------------------------

`promote`, run on a volume node, makes it the primary over the volumes it already holds:

- **The index.** If the old primary still answers, a fresh one is pulled from it first.
  Otherwise the newest `meta/filer-*.meta` is restored, and keys written after that save
  are lost: their bytes stay in the volumes, but no name reaches them.
- **The S3 keys.** An `s3.json` copied into this node's home is kept. Otherwise it is
  rebuilt from the keys a consumer `.env` on this PC already holds -- every consumer has
  them -- so consumers keep their keys and change only the endpoint. With neither, new
  keys are minted and `credentials` prints them for every `.env` on every PC.
- **Copies.** One storage node cannot place `001`, so every bucket drops to `000` (one
  copy), loudly. Pins naming the old primary are dropped. Back to two copies: `install
  --role volume --primary NEW` on a second PC, then `configure-buckets --replication 001`
  on the primary, which also re-copies the single-copy volumes onto the new node.

Every consumer's `.env` names the endpoint by IP, so `point-consumers` rewrites that one
line in each sibling checkout's `.env` on this PC (a dry run until `--apply`).

Planned retirement, both PCs up:

    both PCs:  point-consumers --to NEW --apply    writes now fail instead of landing on
                                                   the old primary after its index is read
    new PC:    promote                             pulls a fresh index, so nothing is lost
    old PC:    demote --primary NEW                or uninstall it; its data is all on NEW

Sudden loss of the primary:

    new PC:    promote, then point-consumers --apply
    old PC:    if it ever boots again, its supervisor finds a peer it recorded running a
               master and rejoins as that peer's volume node; `demote --primary NEW` does
               the same by hand

Restart whatever read the old `.env` at start (the scheduler containers) after a repoint.

Subcommands:

    install --role primary|volume [--primary IP]   download weed, write config, start
    firewall                                        print the one admin-only step
    status [--primary IP]                           every node, its volumes and free slots
    configure-buckets [--pin BUCKET=IP]             primary only: create buckets, placement
                      [--replication 000|001]       the copies the pool can give
    credentials                                     primary only: the consumer .env lines
    backup-meta                                     save the filer index to this node now
    promote [--root DIR]                            volume node: become the primary
    demote --primary IP                             old primary: rejoin as a volume node
    point-consumers [--to IP] [--root DIR] [--apply]  repoint sibling checkouts' .env
    run                                             the supervisor the logon task starts
    stop                                            stop the supervisor and weed

Stdlib only: a fresh PC runs it before any project venv exists. The supervisor runs from
a copy under `%LOCALAPPDATA%\\seaweedfs`, so deleting the checkout that installed it
does not take the pool down. Failures are written to `logs/storage-node.log`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

VERSION = "4.48"
# The large-disk build allows volumes past 32 GB. Every node must run the same build: the
# two disagree on the on-disk offset width.
ASSET = "windows_amd64_large_disk.zip"
RELEASE_URL = f"https://github.com/seaweedfs/seaweedfs/releases/download/{VERSION}/{ASSET}"

HOME = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "seaweedfs"
_HERE = Path(__file__).resolve().parent
# A checkout's `logs/`, or the pool's own when this is the supervisor's copy under HOME.
LOG_FILE = (_HERE.parent if _HERE.name == "scripts" else _HERE) / "logs" / "storage-node.log"
# Where the consumer checkouts sit: beside this one. The supervisor's copy has no checkout.
CONSUMER_ROOT = _HERE.parent.parent if _HERE.name == "scripts" else None
TASK_NAME = "SeaweedFS data-lake pool"

TAILNET = "100.64.0.0/10"
MASTER_PORT = 9333
VOLUME_PORT = 8080
FILER_PORT = 8888
S3_PORT = 8333
# Every node lives on its OS drive, so leave the OS room. Below this the volume server
# marks its volumes read-only rather than filling the disk.
MIN_FREE = "100GiB"
# Smaller than the 30 GB default so a node's free space is used in finer steps and a
# vacuum rewrites less at a time.
VOLUME_SIZE_MB = 8192
DEFAULT_REPLICATION = "001"
# What the pool can give: `001` needs two storage nodes, `000` is all one node can hold.
REPLICATIONS = ("000", "001")
BUCKETS = {"data-lake": "001", "raw-dumps": "000"}
RESTART_DELAY_S = 10
META_BACKUP_EVERY_S = 24 * 3600
META_KEEP = 7
# How often a primary looks for a peer that was promoted over it while it was away.
SUPERSEDE_CHECK_EVERY_S = 300

ENDPOINT_KEY = "ARCHIVE_S3_ENDPOINT_URL"
# Only an endpoint shaped like the pool's is repointed: a consumer on R2 is left alone.
_POOL_URL = re.compile(rf"^http://[^/:\s]+:{S3_PORT}/?$")
_ENV_LINE = re.compile(
    r"^(?P<lead>\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)"
    r"(?P<q>[\"']?)(?P<value>[^\"'#\s]*)(?P=q)(?P<tail>\s*(?:#.*)?)$"
)


@dataclass(frozen=True)
class Node:
    """What this PC is in the pool, persisted to `node.json` for the supervisor."""

    role: str  # "primary" | "volume"
    ip: str  # this PC's Tailscale address
    primary: str  # the primary's Tailscale address (== ip on the primary)
    # The copies the pool can give; read on the primary only. A node.json written before
    # the field existed reads as the two-node default.
    replication: str = DEFAULT_REPLICATION

    def __post_init__(self) -> None:
        if self.role not in ("primary", "volume"):
            raise ValueError(f"role must be primary or volume, not {self.role!r}")
        if self.role == "volume" and self.primary == self.ip:
            raise ValueError("a volume node needs --primary set to the primary's address")
        if self.replication not in REPLICATIONS:
            raise ValueError(f"replication must be one of {REPLICATIONS}")


def promoted(node: Node) -> Node:
    """This volume node as the primary of a pool it is, for now, the only storage node of."""
    return Node(role="primary", ip=node.ip, primary=node.ip, replication="000")


def demoted(node: Node, primary: str) -> Node:
    """This node as a volume node of `primary`."""
    return Node(role="volume", ip=node.ip, primary=primary)


def weed_args(node: Node, home: Path) -> list[str]:
    """The `weed` command line for this node, without the executable."""
    data = str(home / "data")
    # Logging options are weed's own and precede the command; the 1.8 GB default rotation
    # size would let five of them take 9 GB of the OS drive.
    logging = [f"-logdir={home / 'logs'}", "-alsologtostderr=false", "-log_max_size_mb=100"]
    common = [f"-ip={node.ip}", f"-ip.bind={node.ip}"]
    if node.role == "volume":
        return [
            *logging,
            "volume",
            *common,
            f"-mserver={node.primary}:{MASTER_PORT}",
            f"-dir={data}",
            f"-port={VOLUME_PORT}",
            "-max=0",
            f"-minFreeSpace={MIN_FREE}",
        ]
    return [
        *logging,
        "server",
        *common,
        f"-dir={data}",
        f"-master.port={MASTER_PORT}",
        f"-master.defaultReplication={node.replication}",
        f"-master.volumeSizeLimitMB={VOLUME_SIZE_MB}",
        f"-volume.port={VOLUME_PORT}",
        "-volume.max=0",
        f"-volume.minFreeSpace={MIN_FREE}",
        "-filer",
        f"-filer.port={FILER_PORT}",
        "-s3",
        f"-s3.port={S3_PORT}",
        f"-s3.config={home / 's3.json'}",
        # Off: neither catalog nor the IAM/STS API is used, and each is one more surface.
        # Identities come from -s3.config either way.
        "-s3.port.iceberg=0",
        "-s3.port.lance=0",
        "-s3.iam=false",
    ]


def s3_identities(access_key: str, secret_key: str) -> dict:
    """The gateway's identity file: one admin identity every consumer shares."""
    return {
        "identities": [
            {
                "name": "data-lake",
                "credentials": [{"accessKey": access_key, "secretKey": secret_key}],
                "actions": ["Admin", "Read", "Write", "List", "Tagging"],
            }
        ]
    }


def new_credentials() -> tuple[str, str]:
    return secrets.token_hex(10).upper(), secrets.token_urlsafe(30)


def bucket_script(buckets: dict[str, str], pins: dict[str, str] | None = None) -> str:
    """`weed shell` input creating each bucket and setting its placement.

    `pins` maps a bucket to the Tailscale IP of the one node its new volumes go to.
    `fs.configure` replaces a prefix's whole rule, so an unpinned bucket's rule is written
    without `-dataNode` and a pin removed from `pins` is removed from the pool too.
    """
    pins = pins or {}
    lines = []
    for name, replication in buckets.items():
        lines.append(f"s3.bucket.create -name {name}")
        rule = f"fs.configure -locationPrefix=/buckets/{name}/ -replication={replication}"
        if name in pins:
            rule += f" -dataNode={pins[name]}:{VOLUME_PORT}"
        lines.append(f"{rule} -apply")
    return "\n".join(lines) + "\n"


def bucket_replication(buckets: dict[str, str], pool: str) -> dict[str, str]:
    """Each bucket's placement, capped at what the pool can give.

    One storage node cannot place a second copy, and the master does not degrade a `001`
    write to one copy -- it fails it. So a one-node pool writes every bucket as `000`.
    """
    if pool == "000":
        return dict.fromkeys(buckets, "000")
    return dict(buckets)


def reprotect_script(buckets: dict[str, str]) -> str:
    """`weed shell` input giving every volume of each multi-copy bucket its copies again.

    Volumes written while the pool had one node are `000`; relabelling them and running
    the fix copies each onto the other node. Empty when no bucket wants a second copy.
    """
    lines = []
    for name, replication in buckets.items():
        if replication == "000":
            continue
        lines.append(
            f"volume.configure.replication -collectionPattern={name} -replication={replication}"
        )
        lines.append(f"volume.fix.replication -collectionPattern={name} -apply")
    if not lines:
        return ""
    return "\n".join(["lock", *lines, "unlock"]) + "\n"


def merge_pins(
    saved: dict[str, str], requested: list[str], buckets: dict[str, str]
) -> dict[str, str]:
    """Apply `--pin BUCKET=IP` arguments to the saved pins; `BUCKET=` removes one."""
    pins = dict(saved)
    for item in requested:
        name, sep, ip = item.partition("=")
        if not sep or name not in buckets:
            raise ValueError(f"--pin wants BUCKET=IP with BUCKET one of {sorted(buckets)}")
        if ip:
            pins[name] = ip
        else:
            pins.pop(name, None)
    return pins


def drop_pins(pins: dict[str, str], gone: str) -> tuple[dict[str, str], list[str]]:
    """The pins that do not name `gone`, and the buckets whose pin did."""
    kept = {name: ip for name, ip in pins.items() if ip != gone}
    return kept, sorted(set(pins) - set(kept))


def meta_load_script(meta: Path) -> str:
    # weed shell reads a backslash as an escape, so a Windows path loses every one.
    return f"fs.meta.load {meta.as_posix()}\n"


def newest_meta(meta_dir: Path) -> Path | None:
    """The newest non-empty saved index, or None."""
    saved = sorted(p for p in meta_dir.glob("filer-*.meta") if p.stat().st_size)
    return saved[-1] if saved else None


def pool_url(ip: str) -> str:
    """Plain http: the tailnet is already encrypted, and the lens reads the scheme."""
    return f"http://{ip}:{S3_PORT}"


def env_lines(node: Node, access_key: str, secret_key: str) -> list[str]:
    """What a consumer's `.env` needs to archive into the pool."""
    return [
        "ARCHIVE_BACKEND=s3",
        "ARCHIVE_S3_BUCKET=data-lake",
        f"{ENDPOINT_KEY}={pool_url(node.primary)}",
        "ARCHIVE_S3_REGION=us-east-1",
        f"ARCHIVE_S3_ACCESS_KEY_ID={access_key}",
        f"ARCHIVE_S3_SECRET_ACCESS_KEY={secret_key}",
        "ARCHIVE_S3_PREFIX=",
    ]


def env_values(text: str) -> dict[str, str]:
    """The assignments in a `.env`, the last one winning; comments are not assignments."""
    values = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if m:
            values[m["key"]] = m["value"]
    return values


def pool_keys(text: str) -> tuple[str, str] | None:
    """The S3 keys of a `.env` that points at the pool, or None."""
    values = env_values(text)
    if not _POOL_URL.match(values.get(ENDPOINT_KEY, "")):
        return None
    access = values.get("ARCHIVE_S3_ACCESS_KEY_ID", "")
    secret = values.get("ARCHIVE_S3_SECRET_ACCESS_KEY", "")
    return (access, secret) if access and secret else None


def rewrite_endpoint(text: str, url: str) -> tuple[str, int]:
    """`text` with each pool endpoint assignment set to `url`, and how many changed.

    Every other byte is kept: other keys, comments (one naming the key included), quotes,
    a trailing comment and each line's own ending.
    """
    out = []
    changed = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        end = line[len(body) :]
        m = _ENV_LINE.match(body)
        if m and m["key"] == ENDPOINT_KEY and _POOL_URL.match(m["value"]) and m["value"] != url:
            body = f"{m['lead']}{m['q']}{url}{m['q']}{m['tail']}"
            changed += 1
        out.append(body + end)
    return "".join(out), changed


def firewall_script(weed: Path) -> str:
    """The elevated PowerShell that admits the tailnet to weed.exe, and only the tailnet."""
    return (
        f"New-NetFirewallRule -DisplayName '{TASK_NAME}' -Direction Inbound -Action Allow "
        f"-Protocol TCP -Program '{weed}' -RemoteAddress {TAILNET} -Profile Any"
    )


def task_script(pythonw: Path, script: Path) -> str:
    """PowerShell registering the logon task that starts the supervisor, hidden."""
    return "; ".join(
        [
            f"$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '\"{script}\" run'",
            '$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME"',
            "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew",
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t "
            "-Settings $s -Force | Out-Null",
        ]
    )


def _data_nodes(payload: dict) -> Iterator[dict]:
    topology = payload.get("Topology", {})
    for dc in topology.get("DataCenters") or []:
        for rack in dc.get("Racks") or []:
            yield from rack.get("DataNodes") or []


def summarize_topology(payload: dict) -> list[str]:
    """One line per volume server from the master's `/dir/status`."""
    lines = []
    for dn in _data_nodes(payload):
        used, most = dn.get("Volumes", 0), dn.get("Max", 0)
        lines.append(
            f"{dn.get('Url', '?'):24} volumes {used:>4} / {most:<4} "
            f"(~{(most - used) * VOLUME_SIZE_MB / 1024:.0f} GB unallocated)"
        )
    if not lines:
        lines.append("no volume servers registered")
    return lines


def topology_hosts(payload: dict) -> list[str]:
    """The address of each volume server in the master's `/dir/status`."""
    return [dn["Url"].rpartition(":")[0] for dn in _data_nodes(payload) if dn.get("Url")]


def merge_peers(saved: list[str], seen: list[str], own: str) -> list[str]:
    """Every peer ever seen, never this node. Kept when it goes away: that is the case."""
    return sorted({*saved, *seen} - {own})


def superseded_by(node: Node, peers: list[str], answers: Callable[[str], bool]) -> str | None:
    """The peer running a master over this primary, or None.

    Only `promote` starts a master on a peer, so a peer answering on the master port means
    this node was replaced while it was away, and must not run as a second primary.
    """
    if node.role != "primary":
        return None
    for peer in peers:
        if peer != node.ip and answers(peer):
            return peer
    return None


# --- side effects ---------------------------------------------------------------------


def _log_failure(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": message}) + "\n",
        encoding="utf-8",
    )


def _warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def _read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _get_json(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - tailnet node
        return json.load(resp)


def _master_answers(ip: str) -> bool:
    """Whether a master runs at `ip`. A volume node does not listen on the master port."""
    try:
        payload = _get_json(f"http://{ip}:{MASTER_PORT}/cluster/status", timeout=5)
    except (OSError, ValueError):
        return False
    return bool(payload.get("IsLeader") or payload.get("Leader"))


def _tailscale_ip() -> str:
    exe = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


def _download(home: Path) -> Path:
    weed = home / VERSION / "weed.exe"
    if weed.is_file():
        return weed
    weed.parent.mkdir(parents=True, exist_ok=True)
    archive = weed.parent / ASSET
    urllib.request.urlretrieve(RELEASE_URL, archive)  # noqa: S310 - pinned https release URL
    with urllib.request.urlopen(f"{RELEASE_URL}.md5") as resp:  # noqa: S310 - same release
        expected = resp.read().decode().split()[0].lower()
    actual = hashlib.md5(archive.read_bytes()).hexdigest()  # noqa: S324 - release publishes md5
    if actual != expected:
        archive.unlink()
        raise RuntimeError(f"{ASSET}: md5 {actual} does not match the release's {expected}")
    with zipfile.ZipFile(archive) as zf:
        zf.extract("weed.exe", weed.parent)
    return weed


def _weed(home: Path) -> Path:
    return home / VERSION / "weed.exe"


def _read_node(home: Path) -> Node:
    return Node(**json.loads((home / "node.json").read_text(encoding="utf-8")))


def _write_node(home: Path, node: Node) -> None:
    _write_json(home / "node.json", asdict(node))


def _deploy_script(home: Path) -> Path:
    """Copy this script to where the logon task runs it from."""
    script = home / "storage-node.py"
    if Path(__file__).resolve() != script.resolve():
        shutil.copyfile(Path(__file__), script)
    return script


def _restart_supervisor(home: Path) -> None:
    """Start the supervisor again, on the node.json just written and this script's code.

    The logon task ignores a start while one runs (`IgnoreNew`), so the running one is
    ended first -- `install` alone would leave the old role running until the next logon.
    """
    _deploy_script(home)
    stop()
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=True, capture_output=True)


def install(role: str, primary: str | None, home: Path = HOME) -> Node:
    ip = _tailscale_ip()
    prior = _read_node(home) if (home / "node.json").exists() else None
    # A re-install of a one-node primary must not put back a second copy it cannot place.
    replication = DEFAULT_REPLICATION
    if prior is not None and prior.role == role == "primary":
        replication = prior.replication
    node = Node(role=role, ip=ip, primary=primary or ip, replication=replication)
    weed = _download(home)
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    _write_node(home, node)
    if role == "primary" and not (home / "s3.json").exists():
        _write_json(home / "s3.json", s3_identities(*new_credentials()))
    script = _deploy_script(home)
    pythonw = Path(sys.base_prefix) / "pythonw.exe"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", task_script(pythonw, script)], check=True
    )
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=True, capture_output=True)
    print(f"{role} node on {ip}: weed {VERSION} at {weed}, logon task '{TASK_NAME}' started")
    print(f"one admin step remains on this PC -- run: python {Path(__file__)} firewall")
    return node


def shell_args(node: Node, home: Path) -> list[str]:
    """`weed shell` against the primary, naming the filer rather than discovering it.

    Discovery asks the master for a registered filer, and one that has not registered yet
    -- the first half-minute after a start -- fails as "dial tcp: missing address".
    """
    return [
        str(_weed(home)),
        "shell",
        f"-master={node.primary}:{MASTER_PORT}",
        f"-filer={node.primary}:{FILER_PORT}",
    ]


def shell_failed(returncode: int, output: str) -> bool:
    """Whether a `weed shell` run failed, from its exit code and stdout + stderr.

    The exit code alone misleads both ways: a failed command can still exit 0, and an
    existing bucket exits non-zero although it is not a failure -- `configure-buckets` is
    meant to be re-run, and the `fs.configure` after it still re-applies the placement.
    So the `error:` lines decide, and the exit code only when there are none.
    """
    errors = [line for line in output.splitlines() if line.startswith("error:")]
    if not errors:
        return returncode != 0
    return any(not line.endswith("already exists") for line in errors)


def _shell(node: Node, home: Path, script: str, timeout: float = 120) -> None:
    """Feed `script` to `weed shell` on the primary; raise if it reports an error."""
    out = subprocess.run(
        shell_args(node, home),
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    print(out.stdout.strip())
    if shell_failed(out.returncode, out.stdout + out.stderr):
        raise RuntimeError(out.stderr.strip() or "weed shell reported an error (above)")


def _wait_for_filer(node: Node, timeout_s: float = 180) -> None:
    """Block until the filer on `node` answers HTTP at all; it starts after the master."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(f"http://{node.ip}:{FILER_PORT}/", timeout=5):  # noqa: S310 - tailnet
                return
        except urllib.error.HTTPError:
            return  # an error status is still the filer answering
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the filer on {node.ip}:{FILER_PORT} did not answer in {timeout_s:.0f}s: {exc}"
                ) from exc
        time.sleep(3)


def stale_backups(meta_dir: Path, keep: int = META_KEEP) -> list[Path]:
    """The saved indexes past the newest `keep`, oldest first."""
    return sorted(meta_dir.glob("filer-*.meta"))[:-keep]


def backup_meta(home: Path = HOME) -> Path:
    """Save the filer's index to this node: without it the volumes are chunks with no names.

    On a volume node this pulls over the tailnet, which is the point -- the copy is then
    on a different drive from the primary's filer. A failed save leaves no file behind, so
    a broken run never displaces a good copy from the ones kept.
    """
    node = _read_node(home)
    out = home / "meta" / f"filer-{time.strftime('%Y%m%d')}.meta"
    out.parent.mkdir(exist_ok=True)
    result = subprocess.run(
        shell_args(node, home),
        # weed shell reads a backslash as an escape, so a Windows path loses every one.
        input=f"fs.meta.save -o={out.as_posix()}\n",
        capture_output=True,
        text=True,
        timeout=3600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    output = result.stdout + result.stderr
    if shell_failed(result.returncode, output) or not out.is_file() or not out.stat().st_size:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"fs.meta.save from {node.primary} failed: {output.strip()[-500:]}")
    for old in stale_backups(out.parent):
        old.unlink()
    return out


def _check_superseded(home: Path, node: Node, record: bool) -> Node:
    """`node`, or -- if a peer now runs the pool's master -- this node demoted to its volume.

    `record` first adds every volume server this primary's own master sees to
    `peers.json`, which is what a returning old primary checks against.
    """
    if node.role != "primary":
        return node
    peers_file = home / "peers.json"
    peers = _read_json(peers_file, [])
    if record:
        try:
            seen = topology_hosts(_get_json(f"http://{node.ip}:{MASTER_PORT}/dir/status"))
        except (OSError, ValueError):
            seen = []
        merged = merge_peers(peers, seen, node.ip)
        if merged != peers:
            _write_json(peers_file, merged)
            peers = merged
    leader = superseded_by(node, peers, _master_answers)
    if leader is None:
        return node
    new = demoted(node, leader)
    _write_node(home, new)
    _log_failure(
        f"run: {leader} runs the pool's master, so this primary was superseded while away; "
        "it rejoins as a volume node"
    )
    return new


def run(home: Path = HOME) -> None:
    """Keep weed running: restart it whenever it exits, until the task is stopped."""
    now = time.monotonic()
    next_backup = now + 3600
    next_check = now + SUPERSEDE_CHECK_EVERY_S
    while True:
        node = _check_superseded(home, _read_node(home), record=False)
        args = [str(_weed(home)), *weed_args(node, home)]
        proc = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while proc.poll() is None:
            time.sleep(5)
            now = time.monotonic()
            if now >= next_backup:
                next_backup = now + META_BACKUP_EVERY_S
                try:
                    backup_meta(home)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    # The next day's run tries again; the pool itself is unaffected.
                    _log_failure(f"backup-meta: {exc}")
            if node.role == "primary" and now >= next_check:
                next_check = now + SUPERSEDE_CHECK_EVERY_S
                if _check_superseded(home, node, record=True) != node:
                    proc.terminate()
                    proc.wait()
        time.sleep(RESTART_DELAY_S)


def stop() -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "weed.exe"], capture_output=True)


def status(primary: str | None, home: Path = HOME) -> None:
    host = primary or _read_node(home).primary
    payload = _get_json(f"http://{host}:{MASTER_PORT}/dir/status")
    for line in summarize_topology(payload):
        print(line)


def configure_buckets(pin: list[str], home: Path = HOME, replication: str | None = None) -> None:
    node = _read_node(home)
    if node.role != "primary":
        raise RuntimeError("configure-buckets runs on the primary")
    pins_file = home / "pins.json"
    pins = merge_pins(_read_json(pins_file, {}), pin, BUCKETS)
    _write_json(pins_file, pins)
    reprotect = False
    if replication is not None and replication != node.replication:
        if replication != "000":
            hosts = set(topology_hosts(_get_json(f"http://{node.ip}:{MASTER_PORT}/dir/status")))
            if len(hosts) < 2:
                raise RuntimeError(
                    f"{replication} needs a second storage node and the master sees "
                    f"{len(hosts)}: run `install --role volume --primary {node.ip}` on "
                    "another PC first"
                )
            reprotect = True
        node = replace(node, replication=replication)
        _write_node(home, node)
        # The master's default replication is a start flag.
        _restart_supervisor(home)
        _wait_for_filer(node)
    if node.replication == "000":
        _warn("one copy of everything (000): losing this PC's drive loses the archive")
    _shell(node, home, bucket_script(bucket_replication(BUCKETS, node.replication), pins))
    if reprotect:
        # Copying every single-copy volume to the other node takes as long as the bytes do.
        _shell(node, home, reprotect_script(BUCKETS), timeout=12 * 3600)


def _ensure_identity(home: Path, root: Path | None) -> None:
    """Give the gateway an `s3.json`: the one copied here, the consumers' keys, or new keys."""
    s3 = home / "s3.json"
    if s3.exists():
        print("S3 keys: kept the s3.json already in this node's home")
        return
    for env in sorted(root.glob("*/.env")) if root else []:
        keys = pool_keys(env.read_text(encoding="utf-8", errors="replace"))
        if keys:
            _write_json(s3, s3_identities(*keys))
            print(f"S3 keys: the ones {env} holds, so consumers change only the endpoint")
            return
    _write_json(s3, s3_identities(*new_credentials()))
    _warn(
        "no s3.json here and no consumer .env with the pool's keys: minted new ones. Run "
        "`credentials` and put them in every consumer .env on every PC"
    )


def _index_to_restore(home: Path, old: str) -> Path:
    """A fresh index from the old primary while it answers, else the newest saved one."""
    if _master_answers(old):
        fresh = backup_meta(home)
        print(f"index: pulled fresh from {old} ({fresh.name})")
        return fresh
    meta = newest_meta(home / "meta")
    if meta is None:
        raise RuntimeError(
            f"{old} does not answer and {home / 'meta'} holds no saved index: a primary "
            "without one would serve empty buckets over volumes full of nameless chunks"
        )
    saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(meta.stat().st_mtime))
    _warn(
        f"{old} does not answer, so the index restored is the copy saved {saved} "
        f"({meta.name}). Keys written after that are lost: their bytes stay in the "
        "volumes, but no name reaches them"
    )
    return meta


def promote(home: Path = HOME, root: Path | None = None) -> Node:
    """Make this volume node the primary, over the volumes and index copy it holds.

    Re-running it after a failure resumes from `promote-pending.json`; on a primary that
    was never promoted it refuses, since loading an old index over a live one would undo
    every write since.
    """
    node = _read_node(home)
    pending_file = home / "promote-pending.json"
    if node.role == "primary":
        if not pending_file.exists():
            raise RuntimeError("this node is the primary already; promote runs on a volume node")
        pending = _read_json(pending_file, {})
        old, meta = pending["old"], Path(pending["meta"])
        print(f"resuming the promotion over {old}")
    else:
        old = node.primary
        meta = _index_to_restore(home, old)
        _ensure_identity(home, root)
        pins, dropped = drop_pins(_read_json(home / "pins.json", {}), old)
        for bucket in dropped:
            _warn(f"{bucket} was pinned to {old}, which is leaving: unpinned")
        _write_json(home / "pins.json", pins)
        # This node must not take the old primary, still up in a planned handover, for a
        # primary that superseded it.
        peers = [p for p in _read_json(home / "peers.json", []) if p != old]
        _write_json(home / "peers.json", peers)
        _write_json(pending_file, {"old": old, "meta": str(meta)})
        node = promoted(node)
        _write_node(home, node)
        _restart_supervisor(home)
    _wait_for_filer(node)
    _shell(node, home, meta_load_script(meta), timeout=3600)
    pins = _read_json(home / "pins.json", {})
    _shell(node, home, bucket_script(bucket_replication(BUCKETS, node.replication), pins))
    pending_file.unlink()
    print(f"{node.ip} is the primary: master, filer and S3 at {pool_url(node.ip)}")
    _warn(
        "one storage node, so every bucket keeps one copy (000). For two again: `install "
        f"--role volume --primary {node.ip}` on another PC, then here "
        "`configure-buckets --replication 001`"
    )
    print(f"next, on every PC: storage-node.py point-consumers --to {node.ip} --apply")
    if _master_answers(old):
        print(
            f"and on {old}, which still runs a master: storage-node.py demote --primary {node.ip}"
        )
    return node


def demote(primary: str, home: Path = HOME) -> Node:
    """Rejoin the pool as a volume node of `primary`; the volumes here stay and register."""
    node = demoted(_read_node(home), primary)
    _write_node(home, node)
    _restart_supervisor(home)
    print(f"{node.ip} is a volume node of {primary} now")
    return node


def point_consumers(to: str, root: Path, apply: bool) -> list[Path]:
    """Repoint the pool endpoint in each `.env` beside this checkout; a dry run by default."""
    url = pool_url(to)
    changed = []
    for env in sorted(root.glob("*/.env")):
        text = env.read_bytes().decode("utf-8")
        new, count = rewrite_endpoint(text, url)
        if not count:
            continue
        changed.append(env)
        if apply:
            env.write_bytes(new.encode("utf-8"))
        print(f"{'rewrote' if apply else 'would rewrite'} {env}: {ENDPOINT_KEY}={url}")
    if not changed:
        print(f"no .env under {root} names the pool at another address")
    elif not apply:
        print("dry run: nothing written; re-run with --apply")
    else:
        print("restart anything that read the old .env when it started")
    return changed


def credentials(home: Path = HOME) -> None:
    node = _read_node(home)
    ident = json.loads((home / "s3.json").read_text(encoding="utf-8"))["identities"][0]
    cred = ident["credentials"][0]
    print("\n".join(env_lines(node, cred["accessKey"], cred["secretKey"])))


def firewall(home: Path = HOME) -> None:
    print("In an elevated PowerShell (Run as administrator):")
    print(firewall_script(_weed(home)))


def _consumer_root(root: str | None) -> Path:
    if root:
        return Path(root)
    if CONSUMER_ROOT is None:
        raise RuntimeError("run this from a data-lake checkout, or pass --root")
    return CONSUMER_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--role", choices=["primary", "volume"], required=True)
    p_install.add_argument("--primary", help="the primary's Tailscale IP (volume nodes)")
    sub.add_parser("firewall")
    p_status = sub.add_parser("status")
    p_status.add_argument("--primary")
    p_buckets = sub.add_parser("configure-buckets")
    p_buckets.add_argument(
        "--pin", action="append", default=[], help="BUCKET=IP; BUCKET= unpins (repeatable)"
    )
    p_buckets.add_argument(
        "--replication", choices=REPLICATIONS, help="the copies the pool can give"
    )
    p_promote = sub.add_parser("promote")
    p_promote.add_argument("--root", help="where the consumer checkouts are (their .env keys)")
    p_demote = sub.add_parser("demote")
    p_demote.add_argument("--primary", required=True, help="the new primary's Tailscale IP")
    p_point = sub.add_parser("point-consumers")
    p_point.add_argument("--to", help="the primary's Tailscale IP (default: node.json's)")
    p_point.add_argument("--root", help="where the consumer checkouts are")
    p_point.add_argument("--apply", action="store_true", help="write; without it, a dry run")
    for name in ("credentials", "backup-meta", "run", "stop"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    # HOME is read here, not bound as each function's default, so a test can move it.
    commands = {
        "install": lambda: install(args.role, args.primary, HOME),
        "firewall": lambda: firewall(HOME),
        "status": lambda: status(args.primary, HOME),
        "configure-buckets": lambda: configure_buckets(args.pin, HOME, args.replication),
        "credentials": lambda: credentials(HOME),
        "backup-meta": lambda: print(backup_meta(HOME)),
        "promote": lambda: promote(HOME, Path(args.root) if args.root else CONSUMER_ROOT),
        "demote": lambda: demote(args.primary, HOME),
        "point-consumers": lambda: point_consumers(
            args.to or _read_node(HOME).primary, _consumer_root(args.root), args.apply
        ),
        "run": lambda: run(HOME),
        "stop": stop,
    }
    try:
        commands[args.cmd]()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        _log_failure(f"{args.cmd}: {exc}")
        print(f"storage-node {args.cmd} failed: {exc} (see {LOG_FILE})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
