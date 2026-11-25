"""Tests for `scripts/archive-sync.py`, the task that keeps Syncthing running.

No Syncthing and no scheduled task: every command runs against a faked subprocess.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from test_project_scripts import REPO_ROOT, load_script

sync = load_script("archive-sync.py")


@pytest.fixture
def ran(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake(argv, **_kwargs):
        calls.append([str(arg) for arg in argv])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(sync.subprocess, "run", fake)
    return calls


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A PC Syncthing is set up on."""
    exe = tmp_path / "syncthing.exe"
    exe.touch()
    monkeypatch.setattr(sync, "SYNCTHING", exe)
    home = tmp_path / "Syncthing"
    home.mkdir()
    (home / "config.xml").touch()
    return home


def test_the_manifest_claims_the_task_the_script_registers():
    """devkit judges a task by its name in `[schedule]`; a rename of one is of both."""
    manifest = tomllib.loads((REPO_ROOT / ".devkit.toml").read_text(encoding="utf-8"))
    assert sync.TASK_NAME in manifest["schedule"]["tasks"]


def test_serve_neither_opens_a_browser_nor_upgrades_itself(tmp_path):
    args = sync.serve_args(tmp_path)
    assert args[1] == "serve"
    assert {"--no-browser", "--no-upgrade", f"--home={tmp_path}"} <= set(args)


def test_the_task_starts_on_events_alone():
    """A repeating trigger finds the resident supervisor running and is recorded as a
    refused start, which reads as a failing job."""
    script = sync.task_script(Path("C:/py/pythonw.exe"), Path("C:/s/archive-sync.py"))
    assert "-AtLogOn" in script and "EventID=1073" in script
    assert "Repetition" not in script and "-Once" not in script
    assert "IgnoreNew" in script and "-ExecutionTimeLimit 0" in script
    assert "'\"" + str(Path("C:/s/archive-sync.py")) + "\" run'" in script


def test_install_deploys_registers_then_restarts(home, ran):
    sync.install(home)
    assert (home / sync.SCRIPT_NAME).read_bytes() == Path(sync.__file__).read_bytes()
    register, end, shutdown, start = ran
    assert "Register-ScheduledTask" in register[-1]
    assert end[:2] == ["schtasks", "/End"]
    assert shutdown[-2:] == ["operations", "shutdown"]
    assert start[:2] == ["schtasks", "/Run"]


@pytest.mark.parametrize("missing", ["config.xml", "syncthing.exe"])
def test_install_refuses_a_pc_syncthing_is_not_set_up_on(home, ran, missing):
    target = sync.SYNCTHING if missing == "syncthing.exe" else home / missing
    target.unlink()
    with pytest.raises(RuntimeError, match="not set up"):
        sync.install(home)
    assert ran == []


def test_a_refused_registration_stops_nothing(home, monkeypatch):
    calls: list[list[str]] = []

    def refused(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "Access is denied.")

    monkeypatch.setattr(sync.subprocess, "run", refused)
    with pytest.raises(RuntimeError, match="Access is denied"):
        sync.install(home)
    assert len(calls) == 1, "Syncthing is left running under the task it had"


def test_run_ends_an_orphan_then_restarts_syncthing_whenever_it_exits(home, ran, monkeypatch):
    pauses: list[int] = []

    class Stopped(Exception):
        pass

    def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            raise Stopped

    monkeypatch.setattr(sync.time, "sleep", sleep)
    with pytest.raises(Stopped):
        sync.run(home)
    assert ran[0][-2:] == ["operations", "shutdown"]
    assert [call[1] for call in ran[1:]] == ["serve", "serve"]
    assert pauses == [sync.RESTART_DELAY_S] * 2


def test_a_shutdown_that_hangs_is_not_fatal(home, monkeypatch):
    def hang(argv, **_kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(sync.subprocess, "run", hang)
    sync.shut_down(home)


def test_stop_ends_the_supervisor_before_syncthing(home, ran):
    sync.stop(home)
    assert [call[-1] for call in ran] == [sync.TASK_NAME, "shutdown"]


def test_main_reports_a_refusal_and_fails(monkeypatch, capsys):
    def refuse():
        raise RuntimeError("not set up")

    monkeypatch.setattr(sync, "install", refuse)
    assert sync.main(["install"]) == 1
    assert "error: not set up" in capsys.readouterr().err
    monkeypatch.setattr(sync, "stop", lambda: None)
    assert sync.main(["stop"]) == 0
