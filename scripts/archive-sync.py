#!/usr/bin/env python3
"""Keep Syncthing up on a PC that mirrors its archive to the other PCs.

Each PC owns one send-only folder, its own `data/archive`, and receives each other PC's
into `%USERPROFILE%/pool/<host>/archive`. Syncthing's own configuration -- devices,
folders, the Tailscale listen address -- lives in `HOME/config.xml` and is not this
script's: `install` refuses a PC that has none, so it never starts an unconfigured
Syncthing, whose defaults announce it and use public relays.

    install     deploy this script, register the task, restart Syncthing under it
    run         the supervisor the task starts: Syncthing, restarted whenever it exits
    stop        end the supervisor and Syncthing

The task's triggers are a logon and a called-off shutdown, and the supervisor does the
restarting. A repeating trigger is what it had when it was made by hand, and every one of
those fires found the last run going and was recorded by the scheduler as refused.

Standard library only: the task runs it under the machine's Python.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home()))
HOME = _LOCAL / "Syncthing"
SYNCTHING = _LOCAL / "Microsoft" / "WinGet" / "Links" / "syncthing.exe"
TASK_NAME = "Syncthing pool"
SCRIPT_NAME = "archive-sync.py"
RESTART_DELAY_S = 30
SHUTDOWN_TIMEOUT_S = 30
# The System log's "the attempt to restart/shutdown computer failed" (User32, 1073): a
# shutdown ends the supervisor before an application vetoes it, and no logon follows.
# `storage-node.py` has the incident.
SHUTDOWN_ABORTED_QUERY = (
    "<QueryList><Query Id='0' Path='System'><Select Path='System'>"
    "*[System[Provider[@Name='User32'] and EventID=1073]]"
    "</Select></Query></QueryList>"
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def serve_args(home: Path) -> list[str]:
    """Syncthing's command line: no browser, no self-upgrade, its log beside its config."""
    return [
        str(SYNCTHING),
        "serve",
        f"--home={home}",
        "--no-browser",
        "--no-upgrade",
        f"--log-file={home / 'syncthing.log'}",
    ]


def task_script(pythonw: Path, script: Path) -> str:
    """PowerShell registering the task that starts the supervisor, hidden."""
    return "; ".join(
        [
            f"$a = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '\"{script}\" run'",
            '$t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\\$env:USERNAME"',
            # New-ScheduledTaskTrigger has no event trigger; the CIM class is the cmdlet's.
            "$e = Get-CimClass -ClassName MSFT_TaskEventTrigger"
            " -Namespace Root/Microsoft/Windows/TaskScheduler | New-CimInstance -ClientOnly",
            "$e.Enabled = $true",
            f'$e.Subscription = "{SHUTDOWN_ABORTED_QUERY}"',
            "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew",
            f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger @($t, $e) "
            "-Settings $s -Force | Out-Null",
        ]
    )


def shut_down(home: Path) -> None:
    """Ask the Syncthing on `home` to exit; none running is the usual case, not an error."""
    argv = [str(SYNCTHING), "cli", f"--home={home}", "operations", "shutdown"]
    try:
        subprocess.run(
            argv, capture_output=True, timeout=SHUTDOWN_TIMEOUT_S, creationflags=NO_WINDOW
        )
    except subprocess.TimeoutExpired:
        pass  # the supervisor's loop tries its start again until the old one is gone


def stop(home: Path = HOME) -> None:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True)
    shut_down(home)


def run(home: Path = HOME) -> None:
    """Keep Syncthing running until the task is stopped.

    One already running is an orphan -- the task starts one supervisor at a time -- and
    holds the database lock the new one needs, so it is shut down first.
    """
    shut_down(home)
    while True:
        subprocess.run(serve_args(home), creationflags=NO_WINDOW)
        time.sleep(RESTART_DELAY_S)


def install(home: Path = HOME) -> None:
    for needed in (home / "config.xml", SYNCTHING):
        if not needed.is_file():
            raise RuntimeError(f"{needed} is missing: Syncthing is not set up on this PC")
    script = home / SCRIPT_NAME
    if Path(__file__).resolve() != script.resolve():
        shutil.copyfile(__file__, script)
    pythonw = Path(sys.base_prefix) / "pythonw.exe"
    argv = ["powershell", "-NoProfile", "-Command", task_script(pythonw, script)]
    out = subprocess.run(argv, capture_output=True, text=True)
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()
        raise RuntimeError(f"Register-ScheduledTask exited {out.returncode}: {detail}")
    # Re-registering leaves the old instance running, and the task ignores a start while
    # one runs.
    stop(home)
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=True, capture_output=True)
    print(f"task '{TASK_NAME}' registered and started, running {script}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("command", choices=("install", "run", "stop"))
    command = parser.parse_args(argv).command
    try:
        {"install": install, "run": run, "stop": stop}[command]()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
