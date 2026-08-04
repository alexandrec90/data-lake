#!/usr/bin/env python3
"""PreToolUse hook: blocks Bash tool calls that lack an output byte-cap wrapper.

An agent's context is the scarce resource, and one `ls -R` or unfiltered test run
can spend a large slice of it on output nobody reads. This hook makes the cap
mandatory rather than remembered: an uncapped Bash call is blocked with exit 2 and
the reason is fed back into the turn, so the agent re-issues it wrapped.

Two forms pass, and **they do not run in the same shell** -- the block message says
so, because that difference is the most common way the wrapper surprises a caller:

| Form | Shell | Exit code |
| --- | --- | --- |
| `invoke-capped.py --command "..."` | `/bin/sh`; **`cmd.exe` on Windows** | preserved |
| `<command> \\| head -c N` | whatever the harness gives Bash | **masked** (`head`'s) |

The cap size comes from `[bash]` in `.devkit.toml` (see `harness_config.py`),
so a project can widen it without forking this file -- and the number quoted in the
block message follows it, rather than drifting from what the wrapper actually does.

Decision logic is exposed as pure functions (`decide`, `is_capped`, `get_value`) so
it can be unit-tested without spawning a subprocess. See
`scripts/hooks/tests/test_enforce_capped_bash.py`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# scripts/hooks/ on path so the sibling, stdlib-only config helper imports before
# the venv (same pattern as stop.py's harness_config import).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_config

REPO_ROOT = (Path(__file__).parent / "../..").resolve()
CFG = harness_config.load(REPO_ROOT)

# Claude Code hook contract: 0 allows the call, 2 blocks it and feeds stderr back
# to the model. Every other non-zero code is reported as a non-blocking hook
# *error* and the tool call proceeds anyway -- so a blocking hook MUST use 2 and
# MUST write its reason to stderr.
EXIT_BLOCK = 2

# The vendored wrapper's path is fixed by the MANIFEST, so it is safe to match
# literally; `head -c` is the shell-native escape hatch for cases cmd.exe mangles.
ALLOWED_PATTERNS = [
    r"scripts/hooks/invoke-capped\.py",
    r"\|\s*head\s*-c\s*\d+",
]


def block_message(max_bytes: int) -> str:
    """The reason string fed back to the agent, quoting the configured cap."""
    return (
        f"Blocked uncapped Bash command. Route output through a byte-cap wrapper "
        f"(default {max_bytes} bytes).\n"
        f"Suggested pattern: python3 scripts/hooks/invoke-capped.py "
        f'--command "<your command>" --max-bytes {max_bytes}\n'
        "NB: the wrapper runs the command via the platform shell -- cmd.exe on "
        "Windows -- so heredocs, single-quoted paths and escaped alternation do "
        "not survive it. For a pattern search prefer the Grep/Glob tools; for a "
        "command needing POSIX syntax use `<command> | head -c N` instead, which "
        "runs in the harness's own shell but masks the exit code."
    )


def get_value(obj, *paths):
    """Return the first present dotted-path value (as str) from a nested dict."""
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                ok = False
                break
            cur = cur[key]
        if ok and cur is not None:
            return str(cur)
    return None


def is_capped(command: str) -> bool:
    """True if the command already routes output through an allowed cap wrapper."""
    return any(re.search(pattern, command) for pattern in ALLOWED_PATTERNS)


def decide(raw: str, max_bytes: int | None = None) -> tuple[int, str]:
    """Pure decision: map raw stdin payload to (exit_code, message).

    exit_code 0 allows the call, EXIT_BLOCK blocks it. message may be empty.
    `max_bytes` defaults to the manifest value; injectable so a test does not
    depend on the repo it happens to run in.
    """
    cap = CFG.bash.max_bytes if max_bytes is None else max_bytes

    if not raw.strip():
        return 0, ""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0, "enforce-capped-bash: unable to parse hook payload; skipping enforcement."

    tool_name = get_value(payload, "tool_name", "toolName", "tool.name", "name")
    if tool_name != "Bash":
        return 0, ""

    command = get_value(
        payload, "tool_input.command", "toolInput.command", "input.command", "command"
    )
    if not command or not command.strip():
        return (
            EXIT_BLOCK,
            "enforce-capped-bash: Bash tool call is missing command text; blocking by policy.",
        )

    if is_capped(command):
        return 0, ""

    return EXIT_BLOCK, block_message(cap)


def main() -> int:
    exit_code, message = decide(sys.stdin.read())
    if message:
        # stderr, not stdout: only stderr is surfaced for a blocking hook.
        print(message, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
