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

Subcommands:

    install --role primary|volume [--primary IP]   download weed, write config, start
    firewall                                        print the one admin-only step
    status [--primary IP]                           every node, its volumes and free slots
    configure-buckets                               primary only: create buckets, placement
    credentials                                     primary only: the consumer .env lines
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
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
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
BUCKETS = {"data-lake": "001", "raw-dumps": "000"}
RESTART_DELAY_S = 10
META_BACKUP_EVERY_S = 24 * 3600


@dataclass(frozen=True)
class Node:
    """What this PC is in the pool, persisted to `node.json` for the supervisor."""

    role: str  # "primary" | "volume"
    ip: str  # this PC's Tailscale address
    primary: str  # the primary's Tailscale address (== ip on the primary)

    def __post_init__(self) -> None:
        if self.role not in ("primary", "volume"):
            raise ValueError(f"role must be primary or volume, not {self.role!r}")
        if self.role == "volume" and self.primary == self.ip:
            raise ValueError("a volume node needs --primary set to the primary's address")


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
        f"-master.defaultReplication={DEFAULT_REPLICATION}",
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


def bucket_script(buckets: dict[str, str]) -> str:
    """`weed shell` input creating each bucket and pinning its placement."""
    lines = []
    for name, replication in buckets.items():
        lines.append(f"s3.bucket.create -name {name}")
        lines.append(
            f"fs.configure -locationPrefix=/buckets/{name}/ -replication={replication} -apply"
        )
    return "\n".join(lines) + "\n"


def env_lines(node: Node, access_key: str, secret_key: str) -> list[str]:
    """What a consumer's `.env` needs to archive into the pool."""
    return [
        "ARCHIVE_BACKEND=s3",
        "ARCHIVE_S3_BUCKET=data-lake",
        f"ARCHIVE_S3_ENDPOINT_URL=http://{node.primary}:{S3_PORT}",
        "ARCHIVE_S3_REGION=us-east-1",
        f"ARCHIVE_S3_ACCESS_KEY_ID={access_key}",
        f"ARCHIVE_S3_SECRET_ACCESS_KEY={secret_key}",
        "ARCHIVE_S3_PREFIX=",
    ]


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


def summarize_topology(payload: dict) -> list[str]:
    """One line per volume server from the master's `/dir/status`."""
    lines = []
    topology = payload.get("Topology", {})
    for dc in topology.get("DataCenters") or []:
        for rack in dc.get("Racks") or []:
            for dn in rack.get("DataNodes") or []:
                used, most = dn.get("Volumes", 0), dn.get("Max", 0)
                lines.append(
                    f"{dn.get('Url', '?'):24} volumes {used:>4} / {most:<4} "
                    f"(~{(most - used) * VOLUME_SIZE_MB / 1024:.0f} GB unallocated)"
                )
    if not lines:
        lines.append("no volume servers registered")
    return lines


# --- side effects ---------------------------------------------------------------------


def _log_failure(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(
        json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": message}) + "\n",
        encoding="utf-8",
    )


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


def install(role: str, primary: str | None, home: Path = HOME) -> Node:
    ip = _tailscale_ip()
    node = Node(role=role, ip=ip, primary=primary or ip)
    weed = _download(home)
    (home / "data").mkdir(parents=True, exist_ok=True)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    (home / "node.json").write_text(json.dumps(asdict(node), indent=2), encoding="utf-8")
    if role == "primary" and not (home / "s3.json").exists():
        (home / "s3.json").write_text(
            json.dumps(s3_identities(*new_credentials()), indent=2), encoding="utf-8"
        )
    script = home / "storage-node.py"
    shutil.copyfile(Path(__file__), script)
    pythonw = Path(sys.base_prefix) / "pythonw.exe"
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", task_script(pythonw, script)], check=True
    )
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=True, capture_output=True)
    print(f"{role} node on {ip}: weed {VERSION} at {weed}, logon task '{TASK_NAME}' started")
    print(f"one admin step remains on this PC -- run: python {Path(__file__)} firewall")
    return node


def shell_args(node: Node, home: Path) -> list[str]:
    """`weed shell` against this primary, naming the filer rather than discovering it.

    Discovery asks the master for a registered filer, and one that has not registered yet
    -- the first half-minute after a start -- fails as "dial tcp: missing address".
    """
    return [
        str(_weed(home)),
        "shell",
        f"-master={node.ip}:{MASTER_PORT}",
        f"-filer={node.ip}:{FILER_PORT}",
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


def _backup_meta(node: Node, home: Path) -> None:
    """Save the filer's metadata: without it the volumes are chunks with no names."""
    out = home / "meta" / f"filer-{time.strftime('%Y%m%d')}.meta"
    out.parent.mkdir(exist_ok=True)
    subprocess.run(
        shell_args(node, home),
        # weed shell reads a backslash as an escape, so a Windows path loses every one.
        input=f"fs.meta.save -o={out.as_posix()}\n",
        capture_output=True,
        text=True,
        timeout=3600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for old in sorted(out.parent.glob("filer-*.meta"))[:-7]:
        old.unlink()


def run(home: Path = HOME) -> None:
    """Keep weed running: restart it whenever it exits, until the task is stopped."""
    node = _read_node(home)
    args = [str(_weed(home)), *weed_args(node, home)]
    next_backup = time.monotonic() + 3600
    while True:
        proc = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        while proc.poll() is None:
            time.sleep(5)
            if node.role == "primary" and time.monotonic() >= next_backup:
                next_backup = time.monotonic() + META_BACKUP_EVERY_S
                try:
                    _backup_meta(node, home)
                except (OSError, subprocess.SubprocessError):
                    pass  # the next day's run tries again; the pool itself is unaffected
        time.sleep(RESTART_DELAY_S)


def stop() -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "weed.exe"], capture_output=True)


def status(primary: str | None, home: Path = HOME) -> None:
    host = primary or _read_node(home).primary
    with urllib.request.urlopen(f"http://{host}:{MASTER_PORT}/dir/status", timeout=10) as resp:  # noqa: S310 - tailnet master
        payload = json.load(resp)
    for line in summarize_topology(payload):
        print(line)


def configure_buckets(home: Path = HOME) -> None:
    node = _read_node(home)
    if node.role != "primary":
        raise RuntimeError("configure-buckets runs on the primary")
    out = subprocess.run(
        shell_args(node, home),
        input=bucket_script(BUCKETS),
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(out.stdout.strip())
    if shell_failed(out.returncode, out.stdout + out.stderr):
        raise RuntimeError(out.stderr.strip() or "weed shell reported an error (above)")


def credentials(home: Path = HOME) -> None:
    node = _read_node(home)
    ident = json.loads((home / "s3.json").read_text(encoding="utf-8"))["identities"][0]
    cred = ident["credentials"][0]
    print("\n".join(env_lines(node, cred["accessKey"], cred["secretKey"])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--role", choices=["primary", "volume"], required=True)
    p_install.add_argument("--primary", help="the primary's Tailscale IP (volume nodes)")
    sub.add_parser("firewall")
    p_status = sub.add_parser("status")
    p_status.add_argument("--primary")
    for name in ("configure-buckets", "credentials", "run", "stop"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    try:
        # HOME is read here, not bound as each function's default, so a test can move it.
        if args.cmd == "install":
            install(args.role, args.primary, HOME)
        elif args.cmd == "firewall":
            print("In an elevated PowerShell (Run as administrator):")
            print(firewall_script(_weed(HOME)))
        elif args.cmd == "status":
            status(args.primary, HOME)
        elif args.cmd == "configure-buckets":
            configure_buckets(HOME)
        elif args.cmd == "credentials":
            credentials(HOME)
        elif args.cmd == "run":
            run(HOME)
        elif args.cmd == "stop":
            stop()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        _log_failure(f"{args.cmd}: {exc}")
        print(f"storage-node {args.cmd} failed: {exc} (see {LOG_FILE})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
