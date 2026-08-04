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

**Every statement must be capped, not just one.** The check used to be a single
`re.search` over the whole command string, so one capped segment laundered the rest:
`find / -name x; echo done | head -c 10` matched the `head -c` and passed completely
uncapped. The command is now split on top-level `;`, `&&`, `||` and newlines --
quote-aware, so a separator inside `invoke-capped.py --command "a; b"` is not one --
and each statement has to carry its own cap. Within a *pipeline* a cap anywhere
suffices: everything downstream of `head -c N` can only receive N bytes.

**Commands whose output is bounded by a small constant are exempt** (`BOUNDED_COMMANDS`):
`pwd`, `git rev-parse`, `X --version` and friends. The criterion is deliberately strict
-- bounded *regardless of repo or filesystem size* -- which is why `ls`, `cat` and `git
status` are absent despite being the commands most often blocked. Their output scales
with the tree, and the right answer for them is the Read/Glob/Grep tools, which is what
the block message says.

The cap size comes from `[bash]` in `.devkit.toml` (see `harness_config.py`),
so a project can widen it without forking this file -- and the number quoted in the
block message follows it, rather than drifting from what the wrapper actually does.

Decision logic is exposed as pure functions (`decide`, `is_capped`, `statements`,
`is_bounded`, `get_value`) so it can be unit-tested without spawning a subprocess. See
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
WRAPPER_RE = re.compile(r"scripts/hooks/invoke-capped\.py")
HEAD_CAP_RE = re.compile(r"^head\s+-c\s*\d+")

# Statement separators, longest first so `&&` is never read as a bare `&`. A single
# `&` is absent on purpose: backgrounding a command does not bound its output.
STATEMENT_SEPARATORS = ("&&", "||", ";", "\n")

# Command substitution makes any output claim void -- `echo $(find / -name x)` prints
# whatever the substitution found -- so a statement containing one is never bounded.
SUBSTITUTION_RE = re.compile(r"\$\(|`")

# Commands whose output is bounded by a small constant no matter what arguments or
# repository they are given. That is a much stronger claim than "usually short", and it
# is the whole test for membership: `ls`, `cat`, `git status`, `git diff --stat` and
# `git log` all scale with the tree or the history, so none of them are here.
BOUNDED_COMMANDS = tuple(
    re.compile(pattern)
    for pattern in (
        # Fixed, one-line output regardless of flags.
        r"(?:pwd|whoami|hostname|uptime|date|true|false)\b",
        # Prints text that is already in the command, hence already in context.
        r"(?:echo|printf)\s",
        # Silent on success; no output path at all.
        r"(?:cd|export|unset|mkdir|touch)\s",
        # One line: a path, or nothing.
        r"(?:which|type)\s+\S+\s*$",
        r"command\s+-v\s+\S+\s*$",
        # git plumbing that answers with a single ref, hash, or count.
        r"git\s+rev-parse\b",
        r"git\s+branch\s+--show-current\s*$",
        r"git\s+symbolic-ref\b",
        r"git\s+describe\b",
        r"git\s+rev-list\s+--count\b",
        r"git\s+config\s+(?:--\S+\s+)*--get\b",
        # Version probes. `--help` is deliberately excluded: help text is long.
        r"\S+\s+(?:--version|-V)\s*$",
    )
)


def block_message(max_bytes: int) -> str:
    """The reason string fed back to the agent, quoting the configured cap."""
    return (
        f"Blocked uncapped Bash command. Route output through a byte-cap wrapper "
        f"(default {max_bytes} bytes).\n"
        f"Suggested pattern: python3 scripts/hooks/invoke-capped.py "
        f'--command "<your command>" --max-bytes {max_bytes}\n'
        f"--max-bytes must be >= {harness_config.MIN_MAX_BYTES}; below that the "
        "truncation marker crowds out the output it is meant to frame.\n"
        "NB: the wrapper runs the command via the platform shell -- cmd.exe on "
        "Windows -- so heredocs, single-quoted paths and escaped alternation do "
        "not survive it. For a pattern search prefer the Grep/Glob tools; for a "
        "command needing POSIX syntax use `<command> | head -c N` instead, which "
        "runs in the harness's own shell but masks the exit code.\n"
        "Prefer the wrapper for test and lint runs: it keeps a head *and* a tail "
        "window and preserves the exit code, whereas `head -c` keeps the top and "
        "drops the pytest/ruff summary -- the part you actually need.\n"
        "Every statement needs its own cap: in `a; b | head -c N` only `b` is "
        "capped. Commands with constant-size output (pwd, git rev-parse, "
        "--version) are exempt and need no wrapper; ls/cat/git status are not "
        "exempt because their output grows with the tree -- use Read/Glob/Grep."
    )


def split_top_level(text: str, separators: tuple[str, ...]) -> list[str]:
    """Split `text` on `separators` that are outside quotes. Never raises.

    Quote-awareness is the point: `invoke-capped.py --command "cd x; make"` is one
    statement, and a naive split would treat the quoted `;` as a boundary and then
    block a correctly-wrapped command. Not a shell parser -- it tracks single/double
    quotes and backslash escapes, which is what the forms this gate sees actually use.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                buf.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            buf.append(ch)
            buf.append(text[i + 1])
            i += 2
            continue
        hit = next((sep for sep in separators if text.startswith(sep, i)), None)
        if hit is not None:
            out.append("".join(buf))
            buf = []
            i += len(hit)
            continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return [part.strip() for part in out if part.strip()]


def statements(command: str) -> list[str]:
    """The command's top-level statements -- each of which needs its own cap."""
    return split_top_level(command, STATEMENT_SEPARATORS)


def is_bounded(statement: str) -> bool:
    """True when this statement's output is bounded by a small constant."""
    if SUBSTITUTION_RE.search(statement):
        return False
    return any(pattern.match(statement) for pattern in BOUNDED_COMMANDS)


def has_cap(statement: str) -> bool:
    """True when this one statement routes its output through a cap.

    A `head -c N` anywhere in the pipeline counts, not just at the end: everything
    downstream of it can only ever receive N bytes, so `cat big | head -c 100 | grep x`
    is genuinely bounded and blocking it would be a false positive.
    """
    if WRAPPER_RE.search(statement):
        return True
    return any(HEAD_CAP_RE.match(segment) for segment in split_top_level(statement, ("|",)))


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
    """True when EVERY statement in the command is capped or bounded.

    The `all` (rather than the `any` this once was) is the whole fix: a command is
    only as bounded as its least-bounded statement, and the old `re.search` over the
    joined string let `find / -name x; echo done | head -c 10` through.
    """
    parts = statements(command)
    if not parts:
        return False
    return all(is_bounded(part) or has_cap(part) for part in parts)


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
