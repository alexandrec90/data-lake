"""Tests for `scripts/storage-node.py`, the SeaweedFS pool behind the archive.

No weed binary, no Tailscale, no scheduled task: the side-effecting commands run against
faked subprocesses and a temp home. What is pinned is what a plausible edit would break
without anything failing until a PC reboots -- an address the pool would listen on
outside the tailnet, a placement that would silently drop the archive's second copy, a
firewall rule open to the LAN.
"""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from test_project_scripts import load_script

sn = load_script("storage-node.py")

HOME = Path("C:/seaweed")
PRIMARY = sn.Node(role="primary", ip="100.76.121.58", primary="100.76.121.58")
VOLUME = sn.Node(role="volume", ip="100.100.229.119", primary="100.76.121.58")


def _flag(args: list[str], name: str) -> str:
    [value] = [a.split("=", 1)[1] for a in args if a.startswith(f"-{name}=")]
    return value


# --- Node -------------------------------------------------------------------------


def test_unknown_role_is_refused():
    with pytest.raises(ValueError, match="role"):
        sn.Node(role="master", ip="100.1.1.1", primary="100.1.1.1")


def test_volume_node_must_name_a_different_primary():
    """Without --primary a volume node would register with itself and hold no master."""
    with pytest.raises(ValueError, match="--primary"):
        sn.Node(role="volume", ip="100.1.1.1", primary="100.1.1.1")


# --- weed_args --------------------------------------------------------------------


@pytest.mark.parametrize("node", [PRIMARY, VOLUME])
def test_every_role_binds_the_tailscale_address_only(node):
    args = sn.weed_args(node, HOME)
    assert _flag(args, "ip") == node.ip
    assert _flag(args, "ip.bind") == node.ip


@pytest.mark.parametrize("node", [PRIMARY, VOLUME])
def test_logging_options_precede_the_command(node):
    """weed reads logging flags only before the command; after it they are an error."""
    args = sn.weed_args(node, HOME)
    command = args.index("server" if node.role == "primary" else "volume")
    assert all(a.startswith("-log") or a.startswith("-also") for a in args[:command])
    assert any(a.startswith("-logdir=") for a in args[:command])


def test_primary_runs_master_filer_s3_and_defaults_to_two_copies():
    args = sn.weed_args(PRIMARY, HOME)
    assert "server" in args
    assert "-filer" in args and "-s3" in args
    assert _flag(args, "master.defaultReplication") == "001"
    assert _flag(args, "s3.config") == str(HOME / "s3.json")


def test_primary_closes_the_catalog_ports_it_does_not_use():
    args = sn.weed_args(PRIMARY, HOME)
    assert _flag(args, "s3.port.iceberg") == "0"
    assert _flag(args, "s3.port.lance") == "0"
    assert _flag(args, "s3.iam") == "false"


def test_volume_node_registers_with_the_primary():
    args = sn.weed_args(VOLUME, HOME)
    assert "volume" in args
    assert _flag(args, "mserver") == f"{VOLUME.primary}:{sn.MASTER_PORT}"
    assert "-s3" not in args and "-filer" not in args


@pytest.mark.parametrize(
    ("node", "flag"), [(PRIMARY, "volume.minFreeSpace"), (VOLUME, "minFreeSpace")]
)
def test_every_node_reserves_room_on_its_os_drive(node, flag):
    assert _flag(sn.weed_args(node, HOME), flag) == sn.MIN_FREE


# --- buckets and credentials ------------------------------------------------------


def test_archive_keeps_two_copies_and_raw_dumps_one():
    assert sn.BUCKETS == {"data-lake": "001", "raw-dumps": "000"}


def test_bucket_script_creates_then_places_each_bucket():
    script = sn.bucket_script({"a": "001", "b": "000"}).splitlines()
    assert script == [
        "s3.bucket.create -name a",
        "fs.configure -locationPrefix=/buckets/a/ -replication=001 -apply",
        "s3.bucket.create -name b",
        "fs.configure -locationPrefix=/buckets/b/ -replication=000 -apply",
    ]


def test_shell_names_the_filer_instead_of_discovering_it():
    args = sn.shell_args(PRIMARY, HOME)
    assert _flag(args, "filer") == f"{PRIMARY.ip}:{sn.FILER_PORT}"
    assert _flag(args, "master") == f"{PRIMARY.ip}:{sn.MASTER_PORT}"


@pytest.mark.parametrize(
    ("rc", "stdout", "failed"),
    [
        (0, "create bucket under /buckets\n", False),
        (0, "error: get filer configuration: rpc error\n", True),
        (1, "error: bucket data-lake already exists\n", False),
        (1, "error: bucket a already exists\nerror: rpc error\n", True),
        (1, "", True),
    ],
)
def test_shell_failure_is_read_from_stdout_too(rc, stdout, failed):
    assert sn.shell_failed(rc, stdout) is failed


def test_new_credentials_are_unique_and_survive_the_identity_file():
    first, second = sn.new_credentials(), sn.new_credentials()
    assert first != second
    payload = json.loads(json.dumps(sn.s3_identities(*first)))
    cred = payload["identities"][0]["credentials"][0]
    assert (cred["accessKey"], cred["secretKey"]) == first


def test_env_lines_point_consumers_at_the_primary_over_plain_http():
    """Plain http is deliberate: the tailnet is already encrypted, and the lens reads the
    scheme to decide USE_SSL."""
    lines = dict(line.split("=", 1) for line in sn.env_lines(VOLUME, "AK", "SK"))
    assert lines["ARCHIVE_BACKEND"] == "s3"
    assert lines["ARCHIVE_S3_BUCKET"] == "data-lake"
    assert lines["ARCHIVE_S3_ENDPOINT_URL"] == f"http://{VOLUME.primary}:{sn.S3_PORT}"
    assert (lines["ARCHIVE_S3_ACCESS_KEY_ID"], lines["ARCHIVE_S3_SECRET_ACCESS_KEY"]) == (
        "AK",
        "SK",
    )


# --- Windows wiring ---------------------------------------------------------------


def test_firewall_rule_admits_the_tailnet_and_only_weed():
    rule = sn.firewall_script(Path("C:/s/weed.exe"))
    assert f"-RemoteAddress {sn.TAILNET}" in rule
    assert "-Program 'C:" in rule and "weed.exe'" in rule
    assert "-Direction Inbound" in rule and "-Action Allow" in rule


def test_logon_task_runs_the_copied_supervisor_hidden_and_once():
    task = sn.task_script(Path("C:/py/pythonw.exe"), Path("C:/seaweed/storage-node.py"))
    assert "pythonw.exe" in task
    assert "storage-node.py" in task and " run" in task
    assert "-AtLogOn" in task
    assert "-MultipleInstances IgnoreNew" in task
    assert "-ExecutionTimeLimit 0" in task


# --- status -----------------------------------------------------------------------


def test_topology_summary_lists_every_volume_server():
    payload = {
        "Topology": {
            "DataCenters": [
                {
                    "Racks": [
                        {
                            "DataNodes": [
                                {"Url": "100.76.121.58:8080", "Volumes": 2, "Max": 10},
                                {"Url": "100.100.229.119:8080", "Volumes": 0, "Max": 4},
                            ]
                        }
                    ]
                }
            ]
        }
    }
    lines = sn.summarize_topology(payload)
    assert len(lines) == 2
    assert lines[0].startswith("100.76.121.58:8080")
    assert "~64 GB unallocated" in lines[0]
    assert "~32 GB unallocated" in lines[1]


@pytest.mark.parametrize("payload", [{}, {"Topology": {"DataCenters": None}}])
def test_topology_summary_says_so_when_nothing_registered(payload):
    assert sn.summarize_topology(payload) == ["no volume servers registered"]


# --- failure artifact -------------------------------------------------------------


def test_failure_is_written_to_the_log(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "storage-node.log"
    monkeypatch.setattr(sn, "LOG_FILE", log)
    monkeypatch.setattr(sn, "HOME", tmp_path / "missing")
    assert sn.main(["credentials"]) == 1
    record = json.loads(log.read_text(encoding="utf-8"))
    assert record["error"].startswith("credentials:")


# --- commands ---------------------------------------------------------------------


def _home(tmp_path: Path, node) -> Path:
    (tmp_path / "node.json").write_text(json.dumps(asdict(node)), encoding="utf-8")
    return tmp_path


class _Calls:
    """Stands in for `subprocess.run`, recording each argv and answering with `result`."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.argv: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.result = (returncode, stdout, stderr)

    def __call__(self, args, **kwargs):
        self.argv.append(list(args))
        self.inputs.append(kwargs.get("input"))
        return subprocess.CompletedProcess(args, *self.result)


def test_install_writes_the_node_and_credentials_once(tmp_path, monkeypatch):
    calls = _Calls()
    monkeypatch.setattr(sn, "_tailscale_ip", lambda: PRIMARY.ip)
    monkeypatch.setattr(sn, "_download", lambda home: home / "weed.exe")
    monkeypatch.setattr(sn.subprocess, "run", calls)
    node = sn.install("primary", None, tmp_path)
    assert node == PRIMARY
    assert json.loads((tmp_path / "node.json").read_text(encoding="utf-8")) == asdict(PRIMARY)
    first = (tmp_path / "s3.json").read_text(encoding="utf-8")
    assert (tmp_path / "storage-node.py").is_file()
    assert calls.argv[-1][:3] == ["schtasks", "/Run", "/TN"]
    sn.install("primary", None, tmp_path)
    # A re-install must not rotate the keys every consumer's .env already holds.
    assert (tmp_path / "s3.json").read_text(encoding="utf-8") == first


def test_install_of_a_volume_node_mints_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "_tailscale_ip", lambda: VOLUME.ip)
    monkeypatch.setattr(sn, "_download", lambda home: home / "weed.exe")
    monkeypatch.setattr(sn.subprocess, "run", _Calls())
    assert sn.install("volume", VOLUME.primary, tmp_path) == VOLUME
    assert not (tmp_path / "s3.json").exists()


def test_run_restarts_weed_whenever_it_exits(tmp_path, monkeypatch):
    home = _home(tmp_path, VOLUME)
    started: list[list[str]] = []

    class _Exited:
        def poll(self):
            return 1

    def popen(args, **kwargs):
        started.append(args)
        return _Exited()

    class _Stop(Exception):
        pass

    def sleep(seconds):
        if len(started) == 2:
            raise _Stop

    monkeypatch.setattr(sn.subprocess, "Popen", popen)
    monkeypatch.setattr(sn.time, "sleep", sleep)
    with pytest.raises(_Stop):
        sn.run(home)
    assert len(started) == 2
    assert started[0] == [str(sn._weed(home)), *sn.weed_args(VOLUME, home)]


def test_stop_ends_the_task_and_every_weed(monkeypatch):
    calls = _Calls()
    monkeypatch.setattr(sn.subprocess, "run", calls)
    sn.stop()
    assert calls.argv == [
        ["schtasks", "/End", "/TN", sn.TASK_NAME],
        ["taskkill", "/F", "/IM", "weed.exe"],
    ]


def test_status_asks_the_named_primary_and_prints_the_topology(monkeypatch, capsys):
    asked: list[str] = []

    def urlopen(url, timeout):
        asked.append(url)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(sn.urllib.request, "urlopen", urlopen)
    sn.status("100.1.2.3", Path("C:/unused"))
    assert asked == [f"http://100.1.2.3:{sn.MASTER_PORT}/dir/status"]
    assert "no volume servers registered" in capsys.readouterr().out


def test_configure_buckets_refuses_a_volume_node(tmp_path):
    with pytest.raises(RuntimeError, match="primary"):
        sn.configure_buckets(_home(tmp_path, VOLUME))


def test_configure_buckets_feeds_the_bucket_script_to_weed_shell(tmp_path, monkeypatch):
    calls = _Calls(returncode=1, stdout="error: bucket data-lake already exists\n")
    monkeypatch.setattr(sn.subprocess, "run", calls)
    home = _home(tmp_path, PRIMARY)
    sn.configure_buckets(home)
    assert calls.argv == [sn.shell_args(PRIMARY, home)]
    assert calls.inputs == [sn.bucket_script(sn.BUCKETS)]


def test_configure_buckets_raises_when_weed_shell_reports_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sn.subprocess, "run", _Calls(stdout="error: rpc error\n"))
    with pytest.raises(RuntimeError):
        sn.configure_buckets(_home(tmp_path, PRIMARY))


def test_credentials_prints_the_env_lines_for_the_stored_keys(tmp_path, capsys):
    home = _home(tmp_path, PRIMARY)
    (home / "s3.json").write_text(json.dumps(sn.s3_identities("AK", "SK")), encoding="utf-8")
    sn.credentials(home)
    assert capsys.readouterr().out.splitlines() == sn.env_lines(PRIMARY, "AK", "SK")


def test_firewall_prints_the_rule_for_this_homes_weed(capsys):
    sn.firewall(HOME)
    out = capsys.readouterr().out
    assert "elevated PowerShell" in out
    assert sn.firewall_script(sn._weed(HOME)) in out


def test_main_dispatches_each_command(monkeypatch, tmp_path):
    ran: list[str] = []
    monkeypatch.setattr(sn, "HOME", tmp_path)
    monkeypatch.setattr(sn, "stop", lambda: ran.append("stop"))
    monkeypatch.setattr(sn, "status", lambda primary, home: ran.append(f"status {primary}"))
    assert sn.main(["stop"]) == 0
    assert sn.main(["status", "--primary", "100.1.2.3"]) == 0
    assert ran == ["stop", "status 100.1.2.3"]
