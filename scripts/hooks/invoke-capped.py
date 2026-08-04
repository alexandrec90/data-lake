#!/usr/bin/env python3
"""Runs a shell command and caps combined stdout+stderr to stay within context limits.

The companion to `enforce-capped-bash.py`: that hook blocks an uncapped Bash call,
this is what the agent routes the call through instead. Both windows of the cap are
kept -- head and tail -- because the two useful parts of a long command's output are
the first lines (what it was doing) and the last (how it failed).

**The shell is the platform's, not the caller's.** `subprocess.run(shell=True)` uses
`/bin/sh` on POSIX and **`cmd.exe` on Windows**, regardless of which shell the agent
harness otherwise provides. On Windows that means heredocs fail, `'single quotes'`
arrive with the quotes intact, and `\\|` alternation in a grep pattern is not
understood. That is a property of the platform, not a bug here -- but it is the
single most common way this wrapper surprises a caller, so it is also stated in the
block message the hook prints.

Defaults come from `[bash]` in `.devkit.toml` (see `harness_config.py`) so a
project can widen or tighten the cap without forking this file. Explicit CLI flags
still win.

`cap_output` is pure and unit-tested in `scripts/hooks/tests/test_invoke_capped.py`.

Usage: python3 scripts/hooks/invoke-capped.py --command "cmd" [--max-bytes N] [--head-bytes N]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# scripts/hooks/ on path so the sibling, stdlib-only config helper imports before
# the venv (same pattern as stop.py's harness_config import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

# Below this a cap costs more than it saves: the truncation marker alone is ~30
# bytes, and a window too small to hold one error line defeats the purpose.
MIN_MAX_BYTES = 512


def cap_output(data: bytes, max_bytes: int, head_bytes: int) -> bytes:
    """Cap `data` to `max_bytes`, keeping a head window and a tail window."""
    if len(data) <= max_bytes:
        return data
    head_bytes = min(head_bytes, max_bytes)
    tail_bytes = max_bytes - head_bytes
    head = data[:head_bytes]
    tail = data[max(0, len(data) - tail_bytes) :] if tail_bytes > 0 else b""
    skipped = len(data) - max_bytes
    marker = f"\n... [truncated bytes={skipped}] ...\n".encode()
    return head + marker + tail


def run_capped(command: str, max_bytes: int, head_bytes: int) -> tuple[int, bytes]:
    """Run `command` in a shell and return (exit_code, capped combined output)."""
    # shell=True is the whole point: this wrapper caps the output of an arbitrary
    # shell command. The command is agent-supplied tooling, not external input.
    result = subprocess.run(command, shell=True, capture_output=True)  # noqa: S602
    combined = result.stdout + result.stderr
    return result.returncode, cap_output(combined, max_bytes, head_bytes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", required=True)
    parser.add_argument("--max-bytes", type=int, default=CFG.bash.max_bytes)
    parser.add_argument("--head-bytes", type=int, default=CFG.bash.head_bytes)
    args = parser.parse_args(argv)

    if args.max_bytes < MIN_MAX_BYTES:
        print(f"--max-bytes must be >= {MIN_MAX_BYTES}", file=sys.stderr)
        return 1

    exit_code, capped = run_capped(args.command, args.max_bytes, args.head_bytes)

    sys.stdout.buffer.write(capped)
    if capped and not capped.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
