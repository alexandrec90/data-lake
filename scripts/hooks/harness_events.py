#!/usr/bin/env python3
"""Append-only ledger of harness events, so a block is diagnosable without the chat.

Until now, most of what the harness *does to an agent* left its only record in that
agent's conversation: the capped-Bash gate blocked a command, the worktree guard routed
an edit into a box, a box spawn failed -- and the sole account of it was prose in a
session someone else has to be told to go read. The one exception proved the pattern:
`worktree.py`'s reap ledger exists because a box vanished and nothing on the machine
could say which pass took it. This module generalises that ledger to the rest of the
harness, so a devkit-scoped session diagnoses from a file instead of from hearsay.

Format follows the reap ledger, and for its reasons: one line per event, an ISO stamp
followed by tab-separated `key=value` pairs. The file is read by grepping for a project
or a session id months later, which rules out JSON (an interrupted write leaves an
unparseable document) and prose. Append-only, never rotated -- a record that the next
run overwrites is not a record.

Where the ledger lives decides who can write to it, and there are two callers:

- **Workspace scripts** (`worktree-guard.py`) know their devkit checkout by `__file__`
  and pass `root` explicitly.
- **Vendored copies** in consuming projects know nothing about this machine's layout.
  They reach the ledger through `$DEVKIT_DIR` -- the same machine-level seam
  `sync-devkit.py` reads -- and when it is unset (CI, a fresh clone, anyone else's
  machine) `record` is a silent no-op. A diagnostic ledger must never be a reason a
  hook fails, so unlike the drift gate there is no "stamped but unset" failure mode
  here: silence is always the right degradation.

Event names and their fields belong to the writers; this module only owns the format.
`record` never raises -- it is called from hooks on paths where they are already
reporting something else, and from blocking flows that must still block.

Tested in `scripts/hooks/tests/test_harness_events.py`.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

LEDGER = Path("logs") / "harness-events.log"

# A blocked command or a lint finding can be pages long; the ledger wants enough to
# recognise it, not to replay it. Grep the session transcript for the rest.
VALUE_LIMIT = 300


def clean(value: object) -> str:
    """One field value, made safe for the line format: whitespace collapsed (tabs and
    newlines are the field and record separators), truncated, never empty."""
    text = " ".join(str(value).split()) or "-"
    return text[:VALUE_LIMIT]


def event_line(stamp: str, event: str, fields: tuple[tuple[str, object], ...]) -> str:
    """One ledger record. Pure, so the format is testable."""
    pairs = (("event", event), *fields)
    return stamp + "\t" + "\t".join(f"{key}={clean(value)}" for key, value in pairs)


def ledger_path(root: Path | None = None) -> Path | None:
    """Where this machine's ledger is, or None when it has none.

    `root` is for callers that already know the devkit checkout; without it the answer
    comes from `$DEVKIT_DIR`, and unset means this machine keeps no central ledger.
    """
    if root is not None:
        return root / LEDGER
    devkit = os.environ.get("DEVKIT_DIR", "").strip()
    if not devkit:
        return None
    base = Path(devkit)
    if not base.is_dir():
        return None
    return base / LEDGER


def record(
    event: str, fields: tuple[tuple[str, object], ...], root: Path | None = None
) -> Path | None:
    """Append one event. Returns the ledger path, or None when nothing was written.

    Best-effort by contract: every caller is a hook mid-block or a CLI mid-report, and
    failing *their* work over bookkeeping would invert the priorities this file exists
    to serve.
    """
    try:
        path = ledger_path(root)
        if path is None:
            return None
        stamp = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(event_line(stamp, event, fields) + "\n")
    except Exception:
        return None
    return path
