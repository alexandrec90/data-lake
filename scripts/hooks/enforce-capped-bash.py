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
`pwd`, `git rev-parse`, `rm`, `X --version` and friends. The criterion is deliberately
strict -- bounded *regardless of repo or filesystem size* -- which is why `ls`, `cat` and
`git status` are absent despite being the commands most often blocked. Their output scales
with the tree, and the right answer for them is the Read/Glob/Grep tools, which is what
the block message says.

**Three shapes used to be blocked that this gate was never meant to catch**, and each
was worse than an ordinary false positive because the remedy the block message offers
does not resolve any of them:

  - *Shell control flow.* `statements()` splits on `;` and newlines, so a loop arrives
    here shredded into fragments -- `do`, `done`, `fi` -- which can never carry a cap
    and match no bounded command. Every loop and conditional was therefore blocked
    unconditionally, and wrapping one is not an option: the wrapper runs through
    `cmd.exe` on Windows, where bash loop syntax is a parse error. Control keywords are
    now bounded on their own, and a keyword that introduces a command is peeled off so
    the command behind it is judged instead (`do ls -R /` is still blocked, on the `ls`).
  - *Heredoc bodies.* Splitting on newlines turned every line of a `git commit -F -
    <<'EOF'` message into its own "statement", so the prose was evaluated as commands.
    `split_top_level` now consumes the body between the operator and its terminator.
  - *`rm`, `cp`, `mv`.* Silent on success, exactly like the `mkdir`/`touch` already
    exempt, and simply omitted. A setup chain -- `cd x && rm -rf out && mkdir out &&
    <capped run>` -- was blocked by the `rm` alone, and there is no way to cap a command
    that prints nothing.

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

# `-v` / `--verbose` turns every silent-on-success command into per-file output that
# scales with the tree: `rm -rv big/` and `mkdir -pv a/b/c` both print a line per entry.
# Disqualifying the flag is cheaper and more honest than a per-command list of which
# ones grew one, and it closes the same hole for the entries that were already exempt.
#
# Bundled short flags are matched (`-rv`, `-pv`), because that is how anyone actually
# writes it -- requiring a standalone `-v` would have let the exact examples above
# through. It is scoped to `SILENT_ON_SUCCESS` for the mirror-image reason: `-v` means
# *version* to about as many commands as it means verbose, and an unscoped rule revoked
# the long-standing `command -v gh` exemption.
VERBOSE_FLAG_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*v[A-Za-z]*|--verbose)(?=\s|$)")

# Commands with no output path at all when they succeed. Named rather than inlined into
# `BOUNDED_COMMANDS` because `VERBOSE_FLAG_RE` has to be scoped to exactly this family.
SILENT_ON_SUCCESS = re.compile(r"(?:cd|export|unset|mkdir|rmdir|touch|rm|cp|mv|ln|chmod)\s")

# A heredoc's body is data, not commands. Without this the `\n` split turns every line
# of a commit message into a "statement" that is neither bounded nor cappable, so the
# whole call is blocked -- and a heredoc cannot be handed to the wrapper either, because
# it does not survive `cmd.exe`.
#
# This pattern does NOT rule out `<<<` on its own, and assuming it did was a bug worth
# recording: it fails at the first `<` of a here-string, the scan advances one character,
# and then `<<'text'` matches perfectly as a heredoc named `text` -- swallowing every
# statement that followed. `split_top_level` consumes `<<<` whole before ever reaching
# here, which is the only reliable place to make that distinction.
HEREDOC_RE = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<word>[A-Za-z_][\w.-]*)(?P=quote)")

# Shell control flow produces no output of its own. Three shapes, because they need
# different treatment:
#   * CONTROL_ONLY -- the whole statement is a keyword (`done`, `fi`). Bounded.
#   * CONTROL_HEADER -- a `for`/`case` header, whose word list is data. Bounded.
#   * CONTROL_PREFIX -- a keyword introducing a command (`do ls -R /`). Peeled off, and
#     the command behind it is judged on its own merits, so the `ls` still blocks.
CONTROL_ONLY_RE = re.compile(r"(?:do|done|then|else|elif|fi|esac|;;|\{|\}|\(|\))\s*$")
CONTROL_HEADER_RE = re.compile(r"(?:for\s+\w+(?:\s+in\b.*)?|case\s+.*\sin)\s*$")
CONTROL_PREFIX_RE = re.compile(r"^(?:do|then|else|elif|if|while|until|\{)\s+")

# Commands whose output is bounded by a small constant no matter what arguments or
# repository they are given. That is a much stronger claim than "usually short", and it
# is the whole test for membership: `ls`, `cat`, `git status`, `git diff --stat` and
# `git log` all scale with the tree or the history, so none of them are here.
_BOUNDED_PATTERNS = (
    # Fixed, one-line output regardless of flags.
    r"(?:pwd|whoami|hostname|uptime|date|true|false)\b",
    # Prints text that is already in the command, hence already in context.
    r"(?:echo|printf)\s",
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

BOUNDED_COMMANDS = (SILENT_ON_SUCCESS, *(re.compile(p) for p in _BOUNDED_PATTERNS))


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
        "capped. Exempt, and needing no wrapper: constant-size output (pwd, git "
        "rev-parse, --version), commands silent on success (mkdir, rm, cp), and "
        "shell control flow. ls/cat/git status are NOT exempt because their "
        "output grows with the tree -- use Read/Glob/Grep."
    )


def skip_heredoc_bodies(text: str, start: int, delimiters: list[str]) -> int:
    """Index just past the bodies of `delimiters`, beginning at `start`.

    Each body runs to a line whose stripped content is its terminator; an unterminated
    body swallows the rest of the text, which is what a shell does too. Multiple
    delimiters are consumed in order, for `cmd <<A <<B`.
    """
    index = start
    for delimiter in delimiters:
        while index < len(text):
            end = text.find("\n", index)
            line = text[index:] if end == -1 else text[index:end]
            index = len(text) if end == -1 else end + 1
            if line.strip() == delimiter:
                break
    return index


def split_top_level(text: str, separators: tuple[str, ...]) -> list[str]:
    """Split `text` on `separators` that are outside quotes. Never raises.

    Quote-awareness is the point: `invoke-capped.py --command "cd x; make"` is one
    statement, and a naive split would treat the quoted `;` as a boundary and then
    block a correctly-wrapped command. Not a shell parser -- it tracks single/double
    quotes, backslash escapes and heredoc bodies, which is what the forms this gate
    sees actually use.

    Heredoc bodies are dropped rather than split. They are data, and the newline split
    otherwise reads each line of a commit message as its own uncappable statement --
    a shape with no legal spelling at all, since a heredoc cannot be handed to the
    wrapper either.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    pending_heredocs: list[str] = []
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
        if text.startswith("<<<", i):
            # A here-string feeds one word and has no body to skip. Consumed whole so
            # the scan cannot re-enter at the second `<` and read `<<'word'` as a
            # heredoc operator -- which swallowed every statement after it.
            buf.append("<<<")
            i += 3
            continue
        # A heredoc operator only *declares* its delimiter here; the body starts after
        # the end of the current line, which may still carry a `| head -c N`.
        if ch == "<" and (heredoc := HEREDOC_RE.match(text, i)):
            pending_heredocs.append(heredoc.group("word"))
            buf.append(heredoc.group(0))
            i = heredoc.end()
            continue
        if ch == "\n" and pending_heredocs:
            i = skip_heredoc_bodies(text, i + 1, pending_heredocs)
            pending_heredocs = []
            # The newline itself was consumed with the body. Emit the boundary it
            # represents when the caller treats newlines as separators; otherwise the
            # statement simply continues, as it would for any other dropped text.
            if "\n" in separators:
                out.append("".join(buf))
                buf = []
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


def strip_control_prefix(statement: str) -> str:
    """Peel leading control keywords, so `do rm -rf x` is judged as `rm -rf x`.

    Loops reach this function already split on `;`, so the keyword and the command it
    introduces arrive in the same fragment. Judging the command behind the keyword is
    what keeps the guarantee intact: `do ls -R /` still blocks, on the `ls`.
    """
    while (peeled := CONTROL_PREFIX_RE.sub("", statement, count=1)) != statement:
        statement = peeled
    return statement


def is_bounded(statement: str) -> bool:
    """True when this statement's output is bounded by a small constant."""
    if SUBSTITUTION_RE.search(statement):
        return False
    statement = strip_control_prefix(statement.strip())
    if SILENT_ON_SUCCESS.match(statement):
        # Scoped to this family, not applied globally: `-v` means *version* to about as
        # many commands as it means verbose, and an unscoped check revoked the
        # long-standing `command -v gh` exemption two lines below.
        return not VERBOSE_FLAG_RE.search(statement)
    if CONTROL_ONLY_RE.match(statement) or CONTROL_HEADER_RE.match(statement):
        return True
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
