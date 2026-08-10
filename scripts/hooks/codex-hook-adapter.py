#!/usr/bin/env python3
"""Run a Claude-style command hook with Codex-compatible failure semantics.

Claude command hooks commonly use non-zero exit codes to block a tool or stop.
Codex supports exit code 2 for several lifecycle events, but not every shared hook
uses that exact code or event-specific output contract. Generated Codex wiring
therefore routes shared handlers through this adapter so any failure becomes the
structured JSON decision for its event.

The adapter also sets ``CLAUDE_PROJECT_DIR`` for shared hook scripts. Codex runs
hook commands from the session cwd, which may be below the repository root.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_REASON_BYTES = 4000
CONTINUATION_EVENTS = frozenset({"PostToolUse", "SubagentStop", "Stop", "UserPromptSubmit"})
COMMON_STOP_EVENTS = frozenset({"PreCompact", "PostCompact"})


def cap_reason(text: str, max_bytes: int = MAX_REASON_BYTES) -> str:
    """Return a UTF-8 byte-capped head/tail failure reason."""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    marker = b"\n... hook output truncated ...\n"
    remaining = max(0, max_bytes - len(marker))
    head_size = remaining // 2
    tail_size = remaining - head_size
    clipped = raw[:head_size] + marker + raw[-tail_size:]
    return clipped.decode("utf-8", errors="replace")


def failure_output(event: str, reason: str) -> dict:
    """Build the Codex response for a failed underlying hook command."""
    if event == "PreToolUse":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    if event == "PermissionRequest":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "decision": {
                    "behavior": "deny",
                    "message": reason,
                },
            }
        }
    if event in CONTINUATION_EVENTS:
        return {"decision": "block", "reason": reason}
    if event in COMMON_STOP_EVENTS:
        return {"continue": False, "stopReason": reason, "systemMessage": reason}
    return {"systemMessage": reason}


def run_hook(
    event: str,
    command: list[str],
    raw_stdin: str,
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[int, str, str]:
    """Run one hook and return adapter exit code, stdout, and stderr."""
    if not command:
        reason = "Codex hook adapter received no command."
        return 0, json.dumps(failure_output(event, reason)), ""

    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(repo_root)
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            input=raw_stdin,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
        )
    except OSError as exc:
        reason = cap_reason(f"{command[0]} could not start: {exc}")
        return 0, json.dumps(failure_output(event, reason)), ""

    if result.returncode == 0:
        # Codex can mark an otherwise successful hook as failed when the command
        # emits diagnostics on stderr. Hook output belongs on stdout; discard
        # success-only diagnostics while retaining every failure detail below.
        # SessionStart's shared script only prints operational status; it is not
        # model context, and an empty successful response is the most portable
        # contract across Codex releases.
        stdout = "" if event == "SessionStart" else result.stdout
        return 0, stdout, ""

    detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    reason = detail or f"{command[0]} failed with exit code {result.returncode}."
    return 0, json.dumps(failure_output(event, cap_reason(reason))), ""


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    return args


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    raw_stdin = sys.stdin.read()
    exit_code, stdout, stderr = run_hook(args.event, args.command, raw_stdin)
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
