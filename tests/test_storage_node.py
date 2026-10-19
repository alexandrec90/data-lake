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
from email.message import Message
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


def test_bucket_script_pins_only_the_named_bucket_to_its_node():
    script = sn.bucket_script({"a": "001", "b": "000"}, {"b": "100.1.2.3"}).splitlines()
    assert script[1] == "fs.configure -locationPrefix=/buckets/a/ -replication=001 -apply"
    assert script[3] == (
        "fs.configure -locationPrefix=/buckets/b/ -replication=000 "
        f"-dataNode=100.1.2.3:{sn.VOLUME_PORT} -apply"
    )


def test_pins_are_added_replaced_and_removed():
    buckets = {"a": "001", "b": "000"}
    pins = sn.merge_pins({}, ["b=100.1.1.1"], buckets)
    assert pins == {"b": "100.1.1.1"}
    assert sn.merge_pins(pins, ["b=100.2.2.2"], buckets) == {"b": "100.2.2.2"}
    assert sn.merge_pins(pins, ["b="], buckets) == {}
    # No argument keeps what was saved: a plain re-run must not drop a pin.
    assert sn.merge_pins(pins, [], buckets) == pins


@pytest.mark.parametrize("item", ["b", "c=100.1.1.1"])
def test_a_pin_on_no_known_bucket_is_refused(item):
    with pytest.raises(ValueError, match="BUCKET=IP"):
        sn.merge_pins({}, [item], {"a": "001", "b": "000"})


@pytest.mark.parametrize("node", [PRIMARY, VOLUME])
def test_shell_names_the_primarys_filer_instead_of_discovering_it(node):
    """A volume node's shell goes to the primary too: that is how it pulls the index."""
    args = sn.shell_args(node, HOME)
    assert _flag(args, "filer") == f"{node.primary}:{sn.FILER_PORT}"
    assert _flag(args, "master") == f"{node.primary}:{sn.MASTER_PORT}"


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
        sn.configure_buckets([], _home(tmp_path, VOLUME))


def test_configure_buckets_feeds_the_bucket_script_to_weed_shell(tmp_path, monkeypatch):
    calls = _Calls(returncode=1, stdout="error: bucket data-lake already exists\n")
    monkeypatch.setattr(sn.subprocess, "run", calls)
    home = _home(tmp_path, PRIMARY)
    sn.configure_buckets([], home)
    assert calls.argv == [sn.shell_args(PRIMARY, home)]
    assert calls.inputs == [sn.bucket_script(sn.BUCKETS)]


def test_configure_buckets_keeps_a_pin_across_re_runs(tmp_path, monkeypatch):
    """fs.configure replaces a rule whole, so a re-run that forgot the pin would undo it."""
    calls = _Calls()
    monkeypatch.setattr(sn.subprocess, "run", calls)
    home = _home(tmp_path, PRIMARY)
    sn.configure_buckets(["raw-dumps=100.100.229.119"], home)
    sn.configure_buckets([], home)
    pinned = sn.bucket_script(sn.BUCKETS, {"raw-dumps": "100.100.229.119"})
    assert calls.inputs == [pinned, pinned]
    assert json.loads((home / "pins.json").read_text(encoding="utf-8")) == {
        "raw-dumps": "100.100.229.119"
    }


def test_configure_buckets_raises_when_weed_shell_reports_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sn.subprocess, "run", _Calls(stdout="error: rpc error\n"))
    with pytest.raises(RuntimeError):
        sn.configure_buckets([], _home(tmp_path, PRIMARY))


# --- filer index backup -----------------------------------------------------------


def _saving(size: int, **result):
    """A `subprocess.run` stand-in that writes `size` bytes where fs.meta.save was told to."""
    calls = _Calls(**result)

    def run(args, **kwargs):
        target = kwargs["input"].split("-o=", 1)[1].strip()
        Path(target).write_bytes(b"x" * size)
        return calls(args, **kwargs)

    return run, calls


def test_backup_meta_pulls_the_primarys_index_onto_a_volume_node(tmp_path, monkeypatch):
    run, calls = _saving(10)
    monkeypatch.setattr(sn.subprocess, "run", run)
    out = sn.backup_meta(_home(tmp_path, VOLUME))
    assert out.parent == tmp_path / "meta" and out.stat().st_size == 10
    assert calls.argv == [sn.shell_args(VOLUME, tmp_path)]
    assert _flag(calls.argv[0], "filer").startswith(VOLUME.primary)


@pytest.mark.parametrize(
    ("size", "result"),
    [(10, {"stdout": "error: rpc error: connection refused\n"}), (0, {})],
)
def test_a_failed_backup_raises_and_leaves_no_file(tmp_path, monkeypatch, size, result):
    """An empty or errored save must not count as one of the copies kept."""
    run, _ = _saving(size, **result)
    monkeypatch.setattr(sn.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="fs.meta.save"):
        sn.backup_meta(_home(tmp_path, VOLUME))
    assert list((tmp_path / "meta").iterdir()) == []


def test_only_the_newest_backups_are_kept(tmp_path):
    for day in range(1, 11):
        (tmp_path / f"filer-202610{day:02}.meta").write_bytes(b"x")
    stale = sn.stale_backups(tmp_path, keep=7)
    assert [p.name for p in stale] == [f"filer-202610{d:02}.meta" for d in (1, 2, 3)]


def test_run_backs_up_the_index_on_a_volume_node_too(tmp_path, monkeypatch):
    home = _home(tmp_path, VOLUME)
    backed_up: list[Path] = []

    class _Running:
        def poll(self):
            return None

    class _Stop(Exception):
        pass

    def backup(h):
        backed_up.append(h)
        raise _Stop

    monkeypatch.setattr(sn.subprocess, "Popen", lambda args, **kwargs: _Running())
    monkeypatch.setattr(sn.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sn.time, "monotonic", iter([0, 3600, 3600]).__next__)
    monkeypatch.setattr(sn, "backup_meta", backup)
    with pytest.raises(_Stop):
        sn.run(home)
    assert backed_up == [home]


def test_run_logs_a_failed_backup_and_keeps_going(tmp_path, monkeypatch):
    home = _home(tmp_path, VOLUME)
    log = tmp_path / "logs" / "storage-node.log"
    monkeypatch.setattr(sn, "LOG_FILE", log)
    polls = iter([None, 1])

    class _Once:
        def poll(self):
            return next(polls)

    class _Stop(Exception):
        pass

    def sleep(seconds):
        if seconds == sn.RESTART_DELAY_S:
            raise _Stop

    def backup(h):
        raise RuntimeError("fs.meta.save from 100.76.121.58 failed: refused")

    monkeypatch.setattr(sn.subprocess, "Popen", lambda args, **kwargs: _Once())
    monkeypatch.setattr(sn.time, "sleep", sleep)
    monkeypatch.setattr(sn.time, "monotonic", iter([0, 3600, 3600]).__next__)
    monkeypatch.setattr(sn, "backup_meta", backup)
    with pytest.raises(_Stop):
        sn.run(home)
    assert json.loads(log.read_text(encoding="utf-8"))["error"].startswith("backup-meta:")


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
    monkeypatch.setattr(
        sn, "configure_buckets", lambda pin, home, rep: ran.append(f"pin {pin} {rep}")
    )
    monkeypatch.setattr(sn, "backup_meta", lambda home: ran.append("backup") or home)
    monkeypatch.setattr(sn, "promote", lambda home, root: ran.append(f"promote {root}"))
    monkeypatch.setattr(sn, "demote", lambda primary, home: ran.append(f"demote {primary}"))
    monkeypatch.setattr(
        sn,
        "point_consumers",
        lambda to, root, apply: ran.append(f"point {to} {root.name} {apply}"),
    )
    _home(tmp_path, VOLUME)
    assert sn.main(["stop"]) == 0
    assert sn.main(["status", "--primary", "100.1.2.3"]) == 0
    assert sn.main(["configure-buckets", "--pin", "raw-dumps=100.1.1.1"]) == 0
    assert sn.main(["configure-buckets", "--replication", "001"]) == 0
    assert sn.main(["backup-meta"]) == 0
    assert sn.main(["promote", "--root", str(tmp_path / "vs")]) == 0
    assert sn.main(["demote", "--primary", "100.1.2.3"]) == 0
    assert sn.main(["point-consumers", "--root", str(tmp_path / "vs")]) == 0
    assert sn.main(["point-consumers", "--to", "100.4.4.4", "--root", "vs", "--apply"]) == 0
    assert ran == [
        "stop",
        "status 100.1.2.3",
        "pin ['raw-dumps=100.1.1.1'] None",
        "pin [] 001",
        "backup",
        f"promote {tmp_path / 'vs'}",
        "demote 100.1.2.3",
        # The default target is the primary node.json names.
        f"point {VOLUME.primary} vs False",
        "point 100.4.4.4 vs True",
    ]


def test_point_consumers_from_the_supervisors_copy_needs_a_root(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "HOME", _home(tmp_path, VOLUME))
    monkeypatch.setattr(sn, "LOG_FILE", tmp_path / "logs" / "storage-node.log")
    monkeypatch.setattr(sn, "CONSUMER_ROOT", None)
    assert sn.main(["point-consumers"]) == 1
    assert "--root" in json.loads(sn.LOG_FILE.read_text(encoding="utf-8"))["error"]


# --- losing or retiring the primary: the pure parts -----------------------------------

PROMOTED = sn.Node(role="primary", ip=VOLUME.ip, primary=VOLUME.ip, replication="000")


def test_a_node_json_from_before_replication_reads_as_two_copies():
    node = sn.Node(**{"role": "primary", "ip": PRIMARY.ip, "primary": PRIMARY.ip})
    assert node.replication == "001"


def test_a_replication_the_pool_cannot_express_is_refused():
    with pytest.raises(ValueError, match="replication"):
        sn.Node(role="primary", ip="100.1.1.1", primary="100.1.1.1", replication="010")


def test_promoted_node_is_its_own_primary_with_one_copy():
    assert sn.promoted(VOLUME) == PROMOTED


def test_demoted_node_registers_with_the_new_primary():
    assert sn.demoted(PRIMARY, VOLUME.ip) == sn.Node(
        role="volume", ip=PRIMARY.ip, primary=VOLUME.ip
    )


def test_a_promoted_primary_runs_the_primary_services_over_its_own_volume_dir():
    """The volumes it holds must register: same -dir as when it was a volume node."""
    args = sn.weed_args(PROMOTED, HOME)
    assert "server" in args and "-filer" in args and "-s3" in args
    assert _flag(args, "ip") == VOLUME.ip
    assert _flag(args, "dir") == _flag(sn.weed_args(VOLUME, HOME), "dir")
    # One node cannot place a second copy, so its default must not ask for one.
    assert _flag(args, "master.defaultReplication") == "000"


def test_a_one_node_pool_writes_every_bucket_as_one_copy():
    assert sn.bucket_replication(sn.BUCKETS, "000") == {"data-lake": "000", "raw-dumps": "000"}
    assert sn.bucket_replication(sn.BUCKETS, "001") == sn.BUCKETS


def test_pins_naming_the_lost_node_are_dropped_and_named():
    pins = {"raw-dumps": VOLUME.ip, "data-lake": PRIMARY.ip}
    assert sn.drop_pins(pins, PRIMARY.ip) == ({"raw-dumps": VOLUME.ip}, ["data-lake"])
    assert sn.drop_pins(pins, "100.9.9.9") == (pins, [])


def test_meta_load_script_names_the_file_with_forward_slashes():
    assert sn.meta_load_script(Path("C:/seaweed/meta/filer-20261017.meta")) == (
        "fs.meta.load C:/seaweed/meta/filer-20261017.meta\n"
    )


def test_reprotect_script_restores_the_copies_of_multi_copy_buckets_only():
    assert sn.reprotect_script(sn.BUCKETS).splitlines() == [
        "lock",
        "volume.configure.replication -collectionPattern=data-lake -replication=001",
        "volume.fix.replication -collectionPattern=data-lake -apply",
        "unlock",
    ]
    assert sn.reprotect_script({"raw-dumps": "000"}) == ""


def test_newest_meta_skips_empty_saves(tmp_path):
    assert sn.newest_meta(tmp_path) is None
    (tmp_path / "filer-20261016.meta").write_bytes(b"x")
    (tmp_path / "filer-20261017.meta").write_bytes(b"")
    assert sn.newest_meta(tmp_path) == tmp_path / "filer-20261016.meta"


def _topology(*urls: str) -> dict:
    nodes = [{"Url": url, "Volumes": 1, "Max": 2} for url in urls]
    return {"Topology": {"DataCenters": [{"Racks": [{"DataNodes": nodes}]}]}}


def test_topology_hosts_strip_the_port():
    payload = _topology(f"{PRIMARY.ip}:8080", f"{VOLUME.ip}:8080")
    assert sn.topology_hosts(payload) == [PRIMARY.ip, VOLUME.ip]
    assert sn.topology_hosts({}) == []


def test_peers_accumulate_and_never_include_this_node():
    """A peer that stops reporting is kept: a peer gone quiet is the case being checked."""
    merged = sn.merge_peers(["100.3.3.3"], [PRIMARY.ip, VOLUME.ip], PRIMARY.ip)
    assert sorted(merged) == sorted(["100.3.3.3", VOLUME.ip])


def test_a_primary_is_superseded_by_a_peer_running_a_master():
    assert sn.superseded_by(PRIMARY, [VOLUME.ip], lambda ip: True) == VOLUME.ip
    assert sn.superseded_by(PRIMARY, [VOLUME.ip], lambda ip: False) is None
    # Its own master answering is not a rival.
    assert sn.superseded_by(PRIMARY, [PRIMARY.ip], lambda ip: True) is None


def test_a_volume_node_is_never_superseded():
    """Its primary runs a master by design; asking would demote nothing but cost a probe."""
    asked: list[str] = []
    assert sn.superseded_by(VOLUME, [PRIMARY.ip], lambda ip: asked.append(ip) or True) is None
    assert asked == []


# --- consumer .env files ------------------------------------------------------------

NEW_URL = f"http://{VOLUME.ip}:{sn.S3_PORT}"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            f"ARCHIVE_S3_ENDPOINT_URL=http://{PRIMARY.ip}:8333\n",
            f"ARCHIVE_S3_ENDPOINT_URL={NEW_URL}\n",
        ),
        (
            f'export ARCHIVE_S3_ENDPOINT_URL="http://{PRIMARY.ip}:8333"  # pool\r\n',
            f'export ARCHIVE_S3_ENDPOINT_URL="{NEW_URL}"  # pool\r\n',
        ),
        (
            f"ARCHIVE_S3_ENDPOINT_URL = http://{PRIMARY.ip}:8333/",
            f"ARCHIVE_S3_ENDPOINT_URL = {NEW_URL}",
        ),
    ],
)
def test_rewrite_endpoint_changes_only_the_value(before, after):
    assert sn.rewrite_endpoint(before, NEW_URL) == (after, 1)


@pytest.mark.parametrize(
    "line",
    [
        # The note the consumers keep of their R2 days names the key; it is a comment.
        "# R2, before the pool: ARCHIVE_S3_ENDPOINT_URL=https://acct.r2.cloudflarestorage.com\n",
        # A consumer on R2 is not pointed at the pool.
        "ARCHIVE_S3_ENDPOINT_URL=https://acct.r2.cloudflarestorage.com\n",
        f"ARCHIVE_S3_ENDPOINT_URL={NEW_URL}\n",
        f"OTHER_URL=http://{PRIMARY.ip}:8333\n",
    ],
)
def test_rewrite_endpoint_leaves_everything_else(line):
    assert sn.rewrite_endpoint(line, NEW_URL) == (line, 0)


def test_rewrite_endpoint_keeps_every_other_byte():
    text = (
        "# header\r\n"
        "ARCHIVE_BACKEND=s3\r\n"
        "# R2, before the pool: ARCHIVE_S3_ENDPOINT_URL=https://x.r2.cloudflarestorage.com\r\n"
        f"ARCHIVE_S3_ENDPOINT_URL=http://{PRIMARY.ip}:8333\r\n"
        "ARCHIVE_S3_SECRET_ACCESS_KEY=abc=def\r\n"
        "\r\n"
        "TAIL=1"
    )
    new, count = sn.rewrite_endpoint(text, NEW_URL)
    assert count == 1
    assert new == text.replace(f"=http://{PRIMARY.ip}:8333\r\n", f"={NEW_URL}\r\n")


def test_pool_keys_come_only_from_a_env_pointing_at_the_pool():
    pool = (
        f"ARCHIVE_S3_ENDPOINT_URL=http://{PRIMARY.ip}:8333\n"
        "ARCHIVE_S3_ACCESS_KEY_ID=AK\nARCHIVE_S3_SECRET_ACCESS_KEY=SK\n"
    )
    assert sn.pool_keys(pool) == ("AK", "SK")
    r2 = pool.replace(f"http://{PRIMARY.ip}:8333", "https://acct.r2.cloudflarestorage.com")
    assert sn.pool_keys(r2) is None
    assert sn.pool_keys(pool.replace("SK", "")) is None


def test_pool_url_is_the_s3_gateway_over_plain_http():
    assert sn.pool_url(PRIMARY.ip) == f"http://{PRIMARY.ip}:{sn.S3_PORT}"


def test_env_values_reads_assignments_the_last_one_winning():
    text = (
        "# ARCHIVE_BACKEND=local\n"
        "ARCHIVE_BACKEND=local\n"
        'export ARCHIVE_S3_BUCKET="data-lake"  # the archive\r\n'
        "\n"
        "ARCHIVE_BACKEND = s3\n"
    )
    assert sn.env_values(text) == {"ARCHIVE_BACKEND": "s3", "ARCHIVE_S3_BUCKET": "data-lake"}
    assert sn.env_values("# only a comment\n") == {}


def _consumers(root: Path) -> dict[str, Path]:
    envs = {
        "ibkr_trader": (
            "# R2, before the pool: ARCHIVE_S3_ENDPOINT_URL=https://x.r2.cloudflarestorage.com\r\n"
            f"ARCHIVE_S3_ENDPOINT_URL=http://{PRIMARY.ip}:8333\r\n"
            "ARCHIVE_S3_ACCESS_KEY_ID=AK\r\nARCHIVE_S3_SECRET_ACCESS_KEY=SK\r\n"
        ),
        "on-r2": "ARCHIVE_S3_ENDPOINT_URL=https://acct.r2.cloudflarestorage.com\n",
    }
    paths = {}
    for name, text in envs.items():
        (root / name).mkdir(parents=True)
        paths[name] = root / name / ".env"
        paths[name].write_bytes(text.encode("utf-8"))
    return paths


def test_point_consumers_is_a_dry_run_until_applied(tmp_path, capsys):
    envs = _consumers(tmp_path)
    before = {name: path.read_bytes() for name, path in envs.items()}
    assert sn.point_consumers(VOLUME.ip, tmp_path, apply=False) == [envs["ibkr_trader"]]
    assert {name: path.read_bytes() for name, path in envs.items()} == before
    assert "--apply" in capsys.readouterr().out
    sn.point_consumers(VOLUME.ip, tmp_path, apply=True)
    assert envs["ibkr_trader"].read_bytes() == before["ibkr_trader"].replace(
        f"http://{PRIMARY.ip}:8333\r\n".encode(), f"{NEW_URL}\r\n".encode()
    )
    assert envs["on-r2"].read_bytes() == before["on-r2"]
    # Applied twice is applied once.
    assert sn.point_consumers(VOLUME.ip, tmp_path, apply=True) == []


# --- promote, demote and the supervisor -----------------------------------------------


def _promotion(tmp_path: Path, monkeypatch, old_up: bool = False):
    """A volume node's home with a saved index, and every side effect of promote faked."""
    home = tmp_path / "home"
    home.mkdir()
    _home(home, VOLUME)
    (home / "meta").mkdir()
    saved = home / "meta" / "filer-20261017.meta"
    saved.write_bytes(b"index")
    restarts: list[dict] = []

    def restart(h):
        restarts.append(json.loads((h / "node.json").read_text(encoding="utf-8")))

    monkeypatch.setattr(sn, "_restart_supervisor", restart)
    monkeypatch.setattr(sn, "_wait_for_filer", lambda node, timeout_s=180: None)
    monkeypatch.setattr(sn, "_master_answers", lambda ip: old_up and ip == PRIMARY.ip)
    calls = _Calls()
    monkeypatch.setattr(sn.subprocess, "run", calls)
    return home, saved, restarts, calls


def test_promote_after_a_sudden_loss_restores_the_newest_saved_index(tmp_path, monkeypatch, capsys):
    home, saved, restarts, calls = _promotion(tmp_path, monkeypatch)
    root = tmp_path / "vs-code"
    _consumers(root)
    (home / "pins.json").write_text(
        json.dumps({"raw-dumps": VOLUME.ip, "data-lake": PRIMARY.ip}), encoding="utf-8"
    )
    (home / "peers.json").write_text(json.dumps([PRIMARY.ip, "100.3.3.3"]), encoding="utf-8")

    assert sn.promote(home, root) == PROMOTED

    # node.json said primary before the supervisor was restarted on it.
    assert restarts == [asdict(PROMOTED)]
    assert calls.argv == [sn.shell_args(PROMOTED, home)] * 2
    assert calls.inputs == [
        sn.meta_load_script(saved),
        sn.bucket_script({"data-lake": "000", "raw-dumps": "000"}, {"raw-dumps": VOLUME.ip}),
    ]
    assert json.loads((home / "pins.json").read_text(encoding="utf-8")) == {"raw-dumps": VOLUME.ip}
    # The old primary is no rival to this node, even while it is still up.
    assert json.loads((home / "peers.json").read_text(encoding="utf-8")) == ["100.3.3.3"]
    # The consumers' own keys, so their .env changes only the endpoint.
    ident = json.loads((home / "s3.json").read_text(encoding="utf-8"))["identities"][0]
    assert ident["credentials"][0] == {
        "accessKey": "AK",
        "secretKey": "SK",  # pragma: allowlist secret - fixture .env keys, no pool exists
    }
    assert not (home / "promote-pending.json").exists()
    err = capsys.readouterr().err
    assert "Keys written after that are lost" in err
    assert "data-lake was pinned" in err
    assert "--replication 001" in err


def test_a_planned_promote_pulls_a_fresh_index_from_the_live_primary(tmp_path, monkeypatch, capsys):
    home, _, _, calls = _promotion(tmp_path, monkeypatch, old_up=True)
    fresh = home / "meta" / "filer-20261018.meta"

    def backup(h):
        fresh.write_bytes(b"fresh")
        return fresh

    monkeypatch.setattr(sn, "backup_meta", backup)
    sn.promote(home, None)
    assert calls.inputs[0] == sn.meta_load_script(fresh)
    out = capsys.readouterr()
    assert "Keys written after" not in out.err
    assert f"demote --primary {VOLUME.ip}" in out.out


def test_promote_keeps_an_s3_json_copied_over(tmp_path, monkeypatch):
    home, _, _, _ = _promotion(tmp_path, monkeypatch)
    copied = json.dumps(sn.s3_identities("OLD", "KEYS"))
    (home / "s3.json").write_text(copied, encoding="utf-8")
    sn.promote(home, None)
    assert (home / "s3.json").read_text(encoding="utf-8") == copied


def test_promote_with_no_keys_anywhere_mints_new_ones_and_says_so(tmp_path, monkeypatch, capsys):
    home, _, _, _ = _promotion(tmp_path, monkeypatch)
    sn.promote(home, None)
    assert json.loads((home / "s3.json").read_text(encoding="utf-8"))["identities"]
    assert "minted new ones" in capsys.readouterr().err


def test_promote_refuses_the_primary(tmp_path):
    """Loading a saved index over a live filer would undo every write since the save."""
    with pytest.raises(RuntimeError, match="primary already"):
        sn.promote(_home(tmp_path, PRIMARY))


def test_promote_without_any_index_changes_nothing(tmp_path, monkeypatch):
    home, saved, restarts, _ = _promotion(tmp_path, monkeypatch)
    saved.unlink()
    with pytest.raises(RuntimeError, match="no saved index"):
        sn.promote(home, None)
    assert sn._read_node(home) == VOLUME
    assert restarts == []


def test_a_re_run_resumes_an_interrupted_promotion(tmp_path, monkeypatch):
    home, saved, restarts, calls = _promotion(tmp_path, monkeypatch)
    failing = _Calls(stdout="error: rpc error: filer not ready\n")
    monkeypatch.setattr(sn.subprocess, "run", failing)
    with pytest.raises(RuntimeError):
        sn.promote(home, None)
    assert sn._read_node(home) == PROMOTED
    monkeypatch.setattr(sn.subprocess, "run", calls)
    assert sn.promote(home, None) == PROMOTED
    # The supervisor was switched once; the resumed run only loads and configures.
    assert len(restarts) == 1
    assert calls.inputs[0] == sn.meta_load_script(saved)
    assert not (home / "promote-pending.json").exists()


def test_demote_rewrites_the_node_and_restarts_the_supervisor(tmp_path, monkeypatch):
    home = _home(tmp_path, PRIMARY)
    restarts: list[Path] = []
    monkeypatch.setattr(sn, "_restart_supervisor", restarts.append)
    assert sn.demote(VOLUME.ip, home) == sn.demoted(PRIMARY, VOLUME.ip)
    assert sn._read_node(home) == sn.demoted(PRIMARY, VOLUME.ip)
    assert restarts == [home]


def test_restart_supervisor_ends_the_running_one_before_starting_again(tmp_path, monkeypatch):
    """The task ignores a start while one runs, so without the end nothing would switch."""
    calls = _Calls()
    monkeypatch.setattr(sn.subprocess, "run", calls)
    sn._restart_supervisor(tmp_path)
    assert (tmp_path / "storage-node.py").is_file()
    assert calls.argv == [
        ["schtasks", "/End", "/TN", sn.TASK_NAME],
        ["taskkill", "/F", "/IM", "weed.exe"],
        ["schtasks", "/Run", "/TN", sn.TASK_NAME],
    ]


def test_install_keeps_a_one_node_primarys_single_copy(tmp_path, monkeypatch):
    home = _home(tmp_path, PROMOTED)
    monkeypatch.setattr(sn, "_tailscale_ip", lambda: PROMOTED.ip)
    monkeypatch.setattr(sn, "_download", lambda home: home / "weed.exe")
    monkeypatch.setattr(sn.subprocess, "run", _Calls())
    assert sn.install("primary", None, home).replication == "000"


class _Stop(Exception):
    pass


def test_a_returning_primary_that_was_superseded_runs_as_a_volume_node(tmp_path, monkeypatch):
    home = _home(tmp_path, PRIMARY)
    (home / "peers.json").write_text(json.dumps([VOLUME.ip]), encoding="utf-8")
    monkeypatch.setattr(sn, "LOG_FILE", tmp_path / "logs" / "storage-node.log")
    monkeypatch.setattr(sn, "_master_answers", lambda ip: ip == VOLUME.ip)
    started: list[list[str]] = []

    class _Exited:
        def poll(self):
            return 1

    def popen(args, **kwargs):
        started.append(args)
        return _Exited()

    def sleep(seconds):
        raise _Stop

    monkeypatch.setattr(sn.subprocess, "Popen", popen)
    monkeypatch.setattr(sn.time, "sleep", sleep)
    with pytest.raises(_Stop):
        sn.run(home)
    rejoined = sn.demoted(PRIMARY, VOLUME.ip)
    assert started == [[str(sn._weed(home)), *sn.weed_args(rejoined, home)]]
    assert sn._read_node(home) == rejoined
    assert "superseded" in json.loads(sn.LOG_FILE.read_text(encoding="utf-8"))["error"]


def test_a_primary_with_no_rival_runs_as_the_primary(tmp_path, monkeypatch):
    home = _home(tmp_path, PRIMARY)
    (home / "peers.json").write_text(json.dumps([VOLUME.ip]), encoding="utf-8")
    monkeypatch.setattr(sn, "_master_answers", lambda ip: False)
    started: list[list[str]] = []

    class _Exited:
        def poll(self):
            return 1

    def popen(args, **kwargs):
        started.append(args)
        return _Exited()

    def sleep(seconds):
        raise _Stop

    monkeypatch.setattr(sn.subprocess, "Popen", popen)
    monkeypatch.setattr(sn.time, "sleep", sleep)
    with pytest.raises(_Stop):
        sn.run(home)
    assert started == [[str(sn._weed(home)), *sn.weed_args(PRIMARY, home)]]


def test_a_running_primary_records_its_peers_and_stops_when_superseded(tmp_path, monkeypatch):
    """The peer list is what lets a primary that was away notice it was replaced."""
    home = _home(tmp_path, PRIMARY)
    monkeypatch.setattr(sn, "LOG_FILE", tmp_path / "logs" / "storage-node.log")
    monkeypatch.setattr(
        sn,
        "_get_json",
        lambda url, timeout=10: _topology(f"{PRIMARY.ip}:8080", f"{VOLUME.ip}:8080"),
    )
    monkeypatch.setattr(sn, "_master_answers", lambda ip: ip == VOLUME.ip)
    events: list[str] = []

    class _Running:
        def poll(self):
            return 1 if "terminate" in events else None

        def terminate(self):
            events.append("terminate")

        def wait(self):
            events.append("wait")

    def sleep(seconds):
        if seconds == sn.RESTART_DELAY_S:
            raise _Stop

    monkeypatch.setattr(sn.subprocess, "Popen", lambda args, **kwargs: _Running())
    monkeypatch.setattr(sn.time, "sleep", sleep)
    monkeypatch.setattr(sn.time, "monotonic", iter([0, sn.SUPERSEDE_CHECK_EVERY_S]).__next__)
    with pytest.raises(_Stop):
        sn.run(home)
    assert events == ["terminate", "wait"]
    assert json.loads((home / "peers.json").read_text(encoding="utf-8")) == [VOLUME.ip]
    assert sn._read_node(home) == sn.demoted(PRIMARY, VOLUME.ip)


@pytest.mark.parametrize(
    ("body", "answers"),
    [(b'{"IsLeader": true}', True), (b'{"Leader": "100.1.1.1:9333"}', True), (b"{}", False)],
)
def test_master_answers_reads_the_cluster_status(monkeypatch, body, answers):
    monkeypatch.setattr(sn.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(body))
    assert sn._master_answers("100.1.1.1") is answers


def test_a_node_with_no_master_does_not_answer(monkeypatch):
    def refused(url, timeout):
        raise sn.urllib.error.URLError("refused")

    monkeypatch.setattr(sn.urllib.request, "urlopen", refused)
    assert sn._master_answers("100.1.1.1") is False


def test_wait_for_filer_returns_once_it_answers_even_with_an_error(monkeypatch):
    answers = iter(
        [
            sn.urllib.error.URLError("refused"),
            sn.urllib.error.HTTPError("u", 404, "not found", Message(), None),
        ]
    )

    def urlopen(url, timeout):
        raise next(answers)

    monkeypatch.setattr(sn.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sn.time, "sleep", lambda s: None)
    sn._wait_for_filer(PROMOTED)


def test_wait_for_filer_gives_up_at_the_deadline(monkeypatch):
    def urlopen(url, timeout):
        raise sn.urllib.error.URLError("refused")

    monkeypatch.setattr(sn.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sn.time, "sleep", lambda s: None)
    monkeypatch.setattr(sn.time, "monotonic", iter([0, 10, 1000]).__next__)
    with pytest.raises(RuntimeError, match="did not answer"):
        sn._wait_for_filer(PROMOTED, timeout_s=180)


# --- configure-buckets --replication ----------------------------------------------------


def test_a_one_node_pool_configures_every_bucket_as_one_copy(tmp_path, monkeypatch, capsys):
    calls = _Calls()
    monkeypatch.setattr(sn.subprocess, "run", calls)
    sn.configure_buckets([], _home(tmp_path, PROMOTED))
    assert calls.inputs == [sn.bucket_script({"data-lake": "000", "raw-dumps": "000"})]
    assert "one copy" in capsys.readouterr().err


def test_two_copies_are_refused_while_the_master_sees_one_node(tmp_path, monkeypatch):
    home = _home(tmp_path, PROMOTED)
    monkeypatch.setattr(sn, "_get_json", lambda url, timeout=10: _topology(f"{VOLUME.ip}:8080"))
    with pytest.raises(RuntimeError, match="second storage node"):
        sn.configure_buckets([], home, "001")
    assert sn._read_node(home) == PROMOTED


def test_back_to_two_copies_restarts_the_master_and_recopies_the_volumes(tmp_path, monkeypatch):
    home = _home(tmp_path, PROMOTED)
    monkeypatch.setattr(
        sn,
        "_get_json",
        lambda url, timeout=10: _topology(f"{VOLUME.ip}:8080", "100.3.3.3:8080"),
    )
    restarts: list[str] = []
    monkeypatch.setattr(
        sn,
        "_restart_supervisor",
        lambda h: restarts.append(sn._read_node(h).replication),
    )
    monkeypatch.setattr(sn, "_wait_for_filer", lambda node, timeout_s=180: None)
    calls = _Calls()
    monkeypatch.setattr(sn.subprocess, "run", calls)
    sn.configure_buckets([], home, "001")
    assert sn._read_node(home).replication == "001"
    # The master's default is a start flag: the restart must see the new node.json.
    assert restarts == ["001"]
    assert calls.inputs == [sn.bucket_script(sn.BUCKETS, {}), sn.reprotect_script(sn.BUCKETS)]
