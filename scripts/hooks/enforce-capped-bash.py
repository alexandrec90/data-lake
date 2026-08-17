#!/usr/bin/env python3
"""PreToolUse hook: blocks shell calls that lack an output byte-cap wrapper.

An agent's context is the scarce resource, and one `ls -R` or unfiltered test run
can spend a large slice of it on output nobody reads. This hook makes the cap
mandatory rather than remembered: an uncapped Bash call is blocked with exit 2 and
the reason is fed back into the turn, so the agent re-issues it wrapped.

Claude's source configuration is Bash-native. The Codex hook generator adds
``--shell powershell`` to its Windows override, which keeps this one policy while
judging the syntax of the shell that will actually execute the command.

Two forms pass, and **they do not run in the same shell** -- the block message says
so, because that difference is the most common way the wrapper surprises a caller:

| Form | Shell | Exit code |
| --- | --- | --- |
| `invoke-capped.py --command "..."` | `/bin/sh`; **`cmd.exe` on Windows** | preserved |
| `<command> \\| head -c N` | whatever the harness gives Bash | **masked** (`head`'s) |

The second row is a family, not one spelling, and reading it as one spelling was this
gate's most expensive mistake. `tail -c N` is the same byte bound from the other end;
`head -N` and `tail -N` are line bounds; a `> file` redirect is a bound too, and the
strongest one, since the output never reaches the agent at all. Only `head -c N` was
recognised for a long time, and the cost is measurable rather than theoretical --
across this workspace's transcripts, **a little over half of every block this hook has
ever issued was one of the other three**. `CAP_RE` and `REDIRECT_RE` carry the details
and the one deliberate weakening (a line bound does not bound line length).

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

    That fix was written against one spelling of a loop and left four neighbours of it
    blocked, each with the same non-remedy on offer. A `!` between the keyword and the
    condition (`until ! [ -f x ]`, `while ! test -f x`) inverts an exit code and prints
    nothing, so it peels like any other keyword. A redirection *after* `done` binds the
    loop's stdin or stderr rather than emitting anything, so `done < /dev/null` and
    `done 2>/dev/null` are bounded exactly as bare `done` is. A trailing `# comment` is
    not a command at all, and `split_top_level` now drops one -- to end of line, and
    only where a shell would, which is also why the `;` in `pwd # a; ls -R /` stays
    inside the comment instead of starting a statement. And a `case` arm arrives as
    `case $x in a) pwd`: a header and a pattern in front of a command, and both peel.
  - *Heredoc bodies.* Splitting on newlines turned every line of a `git commit -F -
    <<'EOF'` message into its own "statement", so the prose was evaluated as commands.
    `split_top_level` now consumes the body between the operator and its terminator.
  - *`rm`, `cp`, `mv`, `git add`.* Silent on success, exactly like the `mkdir`/`touch`
    already exempt, and simply omitted. A setup chain -- `cd x && rm -rf out && mkdir
    out && <capped run>` -- was blocked by the `rm` alone, and there is no way to cap a
    command that prints nothing. `git add` is the same omission found later and from the
    other end: it blocked the staging step of every commit, and the heredoc carrying the
    message cannot go through the wrapper either.

**And the commit itself, which that fix stopped one command short of** (`COMMIT_LIKE`).
Exempting `git add` made the staging step legal and left `git commit` blocked, so every
commit still cost a block message -- the single most repeated Bash call in the harness,
in a flow (`/ship`) that runs it once per task.

The cap size comes from `[bash]` in `.devkit.toml` (see `harness_config.py`),
so a project can widen it without forking this file -- and the number quoted in the
block message follows it, rather than drifting from what the wrapper actually does.

Decision logic is exposed as pure functions (`decide`, `is_capped`, `statements`,
`is_bounded`, `get_value`) so it can be unit-tested without spawning a subprocess. See
`scripts/hooks/tests/test_enforce_capped_bash.py`.
"""

from __future__ import annotations

import argparse
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
# literally; `head`/`tail` are the shell-native escape hatch for cases cmd.exe mangles.
WRAPPER_RE = re.compile(r"scripts/hooks/invoke-capped\.py")

# What counts as a cap on a pipeline segment. Four spellings, and for a long time only
# the first passed -- which made this gate's single largest source of false positives
# the very escape hatch its own block message recommends. Measured over the workspace's
# transcripts, `head`/`tail` shapes were 40% of every block this hook has ever issued.
#
#   * `head -c N` -- the original, and the only one that used to pass.
#   * `tail -c N` -- the identical byte bound, taken from the other end. Blocking it was
#     a plain oversight, and a self-contradictory one: the block message tells the agent
#     to keep the tail on a test or lint run because the summary at the end "is the part
#     you actually need", and then blocked `pytest ... | tail -c 2500` for doing it.
#   * `head -N` / `head -n N`, and the same two for `tail` -- a *line* bound rather than
#     a byte bound. Weaker, and admitted deliberately. A line count is still bounded
#     regardless of repo or filesystem size, which is this file's stated criterion, and
#     it is the same bound the Read tool applies. What it does not bound is line
#     *length*, so a 50-line cap on minified output can still be large. That residual is
#     worth one certain turn saved per call, and the rewrite the block forced instead
#     (`head -c`) truncates mid-line, which is strictly worse to read.
#
# `tail -n +5` is excluded on purpose -- "from line 5 to the end" is not a bound at all
# -- and so is `tail -f`, which does not terminate. Both fall out of requiring a leading
# `-` on the count rather than accepting any digits.
CAP_RE = re.compile(r"^(?:head|tail)\s+(?:-c\s*\d+|-n\s*\d+|-\d+)(?=\s|$)")

# Codex uses PowerShell on Windows. These are the native equivalents of the Bash
# caps and bounded commands above. They stay separate rather than being mixed into
# the Bash grammar: a token such as `Select-Object` has no output-bound meaning in
# Bash, while `head -c` is not a PowerShell-native pipeline stage.
POWERSHELL_SELECT_CAP_RE = re.compile(
    r"^Select-Object\s+-(?:First|Last)\s+\d+(?=\s|$)", re.IGNORECASE
)
POWERSHELL_OUT_NULL_RE = re.compile(r"^Out-Null(?:\s|$)", re.IGNORECASE)
POWERSHELL_ASSIGNMENT_RE = re.compile(r"^\$[A-Za-z_][\w:]*\s*=")
POWERSHELL_SILENT_RE = re.compile(
    r"^(?:Start-Sleep|Remove-Item|Copy-Item|Move-Item|Set-Content|Add-Content|"
    r"Clear-Content|New-ItemProperty|Set-ItemProperty|Remove-ItemProperty)(?:\s|$)",
    re.IGNORECASE,
)
POWERSHELL_FIXED_RE = re.compile(r"^(?:Test-Path)(?:\s|$)", re.IGNORECASE)
POWERSHELL_CONTENT_CAP_RE = re.compile(
    r"^Get-Content\b.*\s-(?:TotalCount|Tail)\s+\d+(?=\s|$)", re.IGNORECASE
)
POWERSHELL_GET_ITEM_RE = re.compile(r"^Get-Item(?:\s|$)", re.IGNORECASE)
POWERSHELL_GET_ITEM_PROPERTY_RE = re.compile(
    r"^\(\s*Get-Item\b.*\)\.[A-Za-z_]\w*\s*$", re.IGNORECASE
)
POWERSHELL_LENGTH_GUARD_RE = re.compile(
    r"^if\s*\(\s*(?P<value>\$[A-Za-z_]\w*)\.Length\s+-gt\s+(?P<count>\d+)\s*\)"
    r"\s*\{\s*(?P=value)\.Substring\(\s*0\s*,\s*(?P=count)\s*\)\s*\}"
    r"\s*else\s*\{\s*(?P=value)\s*\}\s*$",
    re.IGNORECASE | re.DOTALL,
)

# Redirection of stdout to a file, which bounds a statement by sending its output
# somewhere that is not the agent's context at all -- the strongest bound there is.
# `gh run view --log > <file>` was blocked despite printing nothing, and the remedy on
# offer (cap the output) caps a stream that was never going to arrive.
#
# Written to match only a redirect of *stdout*: `2>&1` and `>&2` are file-descriptor
# duplications and must not count, which is what the leading `(?:^|\s)` and the
# `(?![>&])` do -- the first refuses a preceding fd digit, the second refuses `>&`.
# `1>` and `&>` are spelled out because both do redirect stdout.
#
# What this deliberately does not bound is stderr, which still reaches the terminal. A
# command noisy enough on stderr to matter is rare, and the same latitude is already
# extended to every entry in `SILENT_ON_SUCCESS`.
REDIRECT_RE = re.compile(r"(?:^|\s)(?:1|&)?>>?\s*(?![>&])\S")

# Statement separators, longest first so `&&` is never read as a bare `&`. A single
# `&` is absent on purpose: backgrounding a command does not bound its output.
STATEMENT_SEPARATORS = ("&&", "||", ";", "\n")

# Command substitution makes any output claim void -- `echo $(find / -name x)` prints
# whatever the substitution found -- so a statement containing one is never bounded.
SUBSTITUTION_RE = re.compile(r"\$\(|`")

# ...but only where the shell would actually expand it. Inside single quotes it would
# not, and the difference is not academic: a commit message is prose, and prose about
# this codebase is full of backticked identifiers. `git commit -m 'fix `foo`'` was
# blocked as command substitution -- by its own subject, with a block message that
# names no cause the author could act on.
#
# Double-quoted spans are deliberately left alone, because `$(...)` and backticks DO
# expand inside them. That is the whole distinction, and it is the shell's, not ours.
SINGLE_QUOTED_SPAN_RE = re.compile(r"'[^']*'", re.DOTALL)

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

# Global options that sit between `git` and its subcommand, so the two git exemptions
# below survive `git -C <path> add` as well as `git add`.
#
# That spelling is not exotic here -- it is what the workspace's own ephemeral-box flow
# produces, because the box is never the session's working directory and `git -C <box>`
# is the natural way to reach it. Without this the exemption silently did not apply, and
# the failure is a bad one to leave standing: the block message *promises* the commit
# pair is exempt, so being blocked on a commit reads as the gate being broken rather
# than as the `-C` being the thing it did not recognise.
#
# `-c` and `-C` take a following value, which may be a quoted path with spaces; the
# valued long forms (`--git-dir=`, `--work-tree=`) attach theirs with `=` and are
# covered by the bare `--\S+` arm. None of this weakens the disqualifiers: they are
# `search`es over the whole statement, so a flag cannot hide in the option position.
_GIT_GLOBAL_OPTS = r"""(?:\s+(?:-[cC]\s+(?:"[^"]*"|'[^']*'|\S+)|--\S+))*"""

# Commands with no output path at all when they succeed. Named rather than inlined into
# `BOUNDED_COMMANDS` because `VERBOSE_FLAG_RE` has to be scoped to exactly this family.
#
# `git add` is here for the same reason `rm` and `cp` are, and was found the same way:
# it prints nothing on success, so there is no output to cap and no legal spelling of
# the command that satisfies the gate. It blocked the staging step of every commit --
# `git add -A && git commit -F - <<'EOF' ... ` fails on the `git add` alone, and the
# heredoc that follows cannot be handed to the wrapper either.
#
# `sleep` joins them for the same reason and was found the same way: a poll loop splits
# into `until <test>` / `do sleep 2` / `done`, and once the control keywords became
# bounded the `sleep` was the last fragment in the line still able to block it.
#
# `set`, `shopt`, `trap`, `kill` and `read` were the fragments still able to block it
# after that. They are shell *bookkeeping* -- they change an option, arm a handler,
# signal a pid, consume a line of stdin -- and none of them has an output path when it
# succeeds. `set -euo pipefail` heads a poll loop about as often as the loop keyword
# does, and it was blocked by itself. Each still requires an argument, which is not
# incidental: bare `set` dumps every shell variable and function, so the `\s` is the
# only thing between that spelling and an exemption it must never have.
#
# The three git subcommands beside `add` move a checkout around and answer with a fixed
# confirmation -- "Switched to branch 'x'", or nothing. Bounded by a small constant, not
# by the tree, which is the membership test.
SILENT_ON_SUCCESS = re.compile(
    r"(?:cd|export|unset|mkdir|rmdir|touch|rm|cp|mv|ln|chmod|sleep"
    r"|set|shopt|trap|kill|read|git" + _GIT_GLOBAL_OPTS + r"\s+(?:add|checkout|switch|restore))\s"
)

# The same commands in their argument-less spellings, which are legal shell and equally
# silent: `cd` goes home, `wait` blocks on the background jobs, `read` consumes a line,
# `:` does nothing at all. Kept apart from `SILENT_ON_SUCCESS` rather than relaxing its
# trailing `\s`, because for `set` and `trap` the bare form is the one that *prints* --
# a lone `set` is `env` for shell variables, and relaxing the two families together
# would have exempted it.
BARE_NO_OUTPUT_RE = re.compile(r"(?::|cd|wait|read)\s*$")

# `grep -q` is the other half of a readiness poll -- `until grep -q ready logs/x.log` is
# the shape, and it is the natural spelling once the thing being waited for is a line in
# a file rather than the file itself. `-q` means *print nothing and answer in the exit
# code*, so it is bounded for the same reason `test` is, and by a stronger constant than
# anything else in this file: zero bytes. Without it the loop keyword being exempt bought
# nothing, since the condition it introduces was still blocked.
#
# The flag is looked for outside quoted spans, so a `-q` inside the pattern is not read
# as one, and a pipeline counts as quiet when its *last* stage is: everything upstream
# of `| grep -q x` can only reach `grep`, exactly as for a `head -c` cap.
GREP_RE = re.compile(r"(?:e|f|r)?grep(?:\s|$)")
QUIET_FLAG_RE = re.compile(r"(?:^|\s)(?:-[A-Za-z]*q[A-Za-z]*|--quiet|--silent)(?=\s|$)")

# `git log` is bounded exactly when it is told how many commits to print, which is how
# it is almost always spelled here (`git log --oneline -5`). Bare `git log` scales with
# history and stays blocked, and so does a counted log asked for patches -- one commit's
# diff has no bound at all, so `-p` revokes the exemption the count would have earned.
GIT_LOG_RE = re.compile(r"git" + _GIT_GLOBAL_OPTS + r"\s+log(?:\s|$)")
GIT_LOG_COUNT_RE = re.compile(r"(?:^|\s)(?:-\d+|-n\s*\d+|--max-count(?:=|\s+)\d+)(?=\s|$)")
GIT_LOG_PATCH_RE = re.compile(r"(?:^|\s)(?:-p|-u|--patch)(?=\s|$)")

# Condition tests, pulled out of `_BOUNDED_PATTERNS` because they have to be judged
# *before* the command-substitution veto rather than after it. `test`, `[` and `[[`
# have no stdout path at all, so a substitution inside one feeds the condition and
# never the terminal: `until [ "$(docker inspect --format ... )" = healthy ]` is the
# shape, it is how every readiness poll in this workspace is written, and the veto
# blocked all of them on output that does not exist.
NO_STDOUT_RE = re.compile(r"(?:test|\[\[?)\s")

# Commands that carry an authored message and answer with a fixed-shape summary. They
# are exempt for a stronger reason than the silent-on-success family above: there is no
# spelling of them this gate would accept.
#
#   * The message is multi-line -- a heredoc, a `"..."` spanning newlines, or PowerShell's
#     `@'...'@` -- and none of those survive the wrapper's `cmd.exe`.
#   * So the only remaining escape is `| head -c N`, which **masks the exit code**. On a
#     commit that is actively dangerous: a commit rejected by a pre-commit hook reports
#     success, and the agent ships a branch with nothing on it. The vendored suite's own
#     example of a "legal" commit (`test_git_add_before_a_heredoc_commit_is_allowed`)
#     pipes through `head -c 500` and would swallow exactly that.
#
# What they print is bounded by the *change*, not by the tree: a header line, one stat
# line, and a `create mode` line per newly added path for a commit; a single URL for
# `gh pr create`. That is the same criterion that keeps `ls` and `git status` out.
#
# `gh pr edit` and `gh pr comment` are the same command wearing a later timestamp -- an
# authored `--body-file`, one URL back -- and were left out only because `create` was
# the one spelling in front of whoever wrote this. Correcting a PR body it had just
# written is where the omission surfaced. `gh pr view` is deliberately NOT here: it
# *prints* the body rather than supplying one, and a long PR description is exactly the
# unbounded output this gate is for.
COMMIT_LIKE = re.compile(
    r"(?:git" + _GIT_GLOBAL_OPTS + r"\s+commit|gh\s+pr\s+(?:create|edit|comment))(?:\s|$)"
)

# The two spellings of `git commit` that are `git status` wearing a different name:
# `--dry-run` (with its `--short`/`--porcelain`/`--long` output modes) lists every
# untracked path, and `-v` appends the full staged diff. `VERBOSE_FLAG_RE` covers `-v`.
COMMIT_LIKE_DISQUALIFIERS = re.compile(r"(?:^|\s)--(?:dry-run|short|porcelain|long)(?=\s|$)")

# Quoted spans, removed before those flags are looked for. Without this the *message*
# decides: `git commit -m "Add a --verbose flag"` reads as a verbose commit and is
# blocked, which is a false positive triggered by prose and impossible to diagnose from
# the block message. Flags live outside the quotes; the message is entirely inside them.
QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", re.DOTALL)

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
#
# CONTROL_ONLY carries an optional tail of redirections, because a loop's redirections
# attach to its `done`: `while read -r l; do echo $l; done < paths.txt` is how the
# construct is spelled, and the fragment that reached here was `done < paths.txt`. A
# `<` moves stdin and `2>` moves stderr, so neither is covered by `REDIRECT_RE` -- which
# is right, since neither *bounds* anything, but they do not emit anything either and the
# keyword they trail is already bounded.
#
# `!` is a CONTROL_PREFIX for the same reason `until` is: it inverts the exit status of
# what follows and contributes no output of its own. `until ! [ -f x ]` and `if ! test
# -f x` are ordinary spellings of a poll and a guard, and both were blocked on the `!`
# alone -- the peel loop stopped at it, so the condition behind it was never judged.
CONTROL_ONLY_RE = re.compile(
    r"(?:do|done|then|else|elif|fi|esac|;;|\{|\}|\(|\))(?:\s+\d*[<>]{1,2}&?\s*\S+)*\s*$"
)
CONTROL_HEADER_RE = re.compile(r"(?:for\s+\w+(?:\s+in\b.*)?|case\s+.*\sin)\s*$")
CONTROL_PREFIX_RE = re.compile(r"^(?:do|then|else|elif|if|while|until|\{|!)\s+")

# A `case` arm reaches here as one fragment holding a header, a pattern and a command --
# `case $x in a) pwd` -- because `;;` contains the `;` that `statements()` splits on.
# Peeling both leaves `pwd` to be judged, which is the same trade CONTROL_PREFIX makes:
# a pattern list emits nothing, and an arm whose body is `ls -R /` still blocks on the
# `ls`. The pattern arm requires a `)` followed by whitespace and forbids `(` before it,
# so a subshell (`(cd x && ls)`) and a substitution are not mistaken for one.
#
# Quote characters are excluded from the pattern for a sharper reason than tidiness: a
# `)` inside a *message* would otherwise make an arm out of prose, and
# `git commit -m "fix x) here"` would have been peeled down to `here"` and blocked --
# the exact class of false positive `QUOTED_SPAN_RE` exists to prevent elsewhere.
CASE_HEADER_RE = re.compile(r"^case\s+\S+\s+in\s+")
CASE_ARM_RE = re.compile(r"""^[^()&|;'"]+(?:\|[^()&|;'"]+)*\)\s+""")

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
    # One line: a URL, or a merge base's sha. `--is-ancestor` prints nothing and answers
    # in the exit code, which is the spelling that reached here blocked.
    r"git\s+remote\s+get-url\b",
    r"git\s+merge-base\b",
    # A syntax check: silent on success, one diagnostic on failure.
    r"(?:ba|z)?sh\s+-n\s",
    # Version probes. `--help` is deliberately excluded: help text is long.
    r"\S+\s+(?:--version|-V)\s*$",
)

BOUNDED_COMMANDS = (SILENT_ON_SUCCESS, NO_STDOUT_RE, *(re.compile(p) for p in _BOUNDED_PATTERNS))


def block_message(max_bytes: int, command_shell: str = "bash") -> str:
    """The reason string fed back to the agent, quoting the configured cap."""
    if command_shell == "powershell":
        return (
            "Blocked uncapped PowerShell command. Route output through the "
            f"PowerShell-native byte-cap wrapper (default {max_bytes} bytes).\n"
            "Suggested pattern: python3 scripts/hooks/invoke-capped.py --shell "
            f"powershell --command '<your command>' --max-bytes {max_bytes}\n"
            f"--max-bytes must be >= {harness_config.MIN_MAX_BYTES}.\n"
            "Native caps such as `Select-Object -First N`, `Select-Object -Last N`, "
            "`Get-Content -TotalCount N`, `Get-Content -Tail N`, `Out-Null`, and "
            "stdout redirection are accepted. Every semicolon-separated statement "
            "must be bounded. Raw Get-Content/Get-ChildItem/git status output still "
            "scales with the file or tree and requires a cap."
        )
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
        "command needing POSIX syntax pipe into head/tail instead, which runs in "
        "the harness's own shell but masks the exit code.\n"
        "Any of these count as a cap on a pipeline: `head -c N`, `tail -c N`, "
        "`head -N`, `tail -N` (and their `-n N` spellings), or redirecting stdout "
        "to a file. Prefer the wrapper for test and lint runs even so: it keeps a "
        "head *and* a tail window and preserves the exit code.\n"
        "Every statement needs its own cap: in `a; b | head -c N` only `b` is "
        "capped. Exempt, and needing no wrapper: constant-size output (pwd, git "
        "rev-parse, --version), commands silent on success (mkdir, rm, cp, sleep, "
        "set, trap, kill), condition tests including a quiet `grep -q`, "
        "`git log` given a commit count, the commit pair whose "
        "message cannot survive the wrapper (git add/commit, gh pr "
        "create/edit/comment), and "
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
        if ch == "#" and (i == 0 or text[i - 1].isspace()):
            # A comment is not a command, and it runs to end of line -- so it also
            # swallows any separator inside it, which is why it is dropped here rather
            # than per-statement afterwards: in `pwd # a; ls -R /` the shell never sees
            # a second statement. Only a `#` that starts a word is one; `logs/x#2` and
            # `curl http://h/p#frag` are ordinary arguments and are left alone.
            newline = text.find("\n", i)
            i = len(text) if newline == -1 else newline
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

    The loop runs to a fixed point because the prefixes stack: `until ! [ -f x ]` is two
    of them, and peeling only the first leaves a `!` in front of a condition test that
    would then match nothing.
    """
    while True:
        peeled = CASE_ARM_RE.sub("", CASE_HEADER_RE.sub("", statement, count=1), count=1)
        peeled = CONTROL_PREFIX_RE.sub("", peeled, count=1)
        if peeled == statement:
            return statement
        statement = peeled


def strip_quoted(statement: str) -> str:
    """`statement` with quoted spans blanked out, so flags are read but prose is not."""
    return QUOTED_SPAN_RE.sub(" ", statement)


def is_quiet_grep(statement: str) -> bool:
    """True when the statement's output all ends in a `grep -q`, which prints nothing.

    Judged on the *last* stage of the pipeline, because that is the only one whose
    output can reach the terminal: in `docker ps | grep -q db-1` everything the first
    stage prints is consumed by the second, exactly as a `head -c` cap consumes it.
    """
    segments = split_top_level(statement, ("|",))
    last = segments[-1].strip() if segments else ""
    return bool(GREP_RE.match(last)) and bool(QUIET_FLAG_RE.search(strip_quoted(last)))


def is_bounded(statement: str) -> bool:
    """True when this statement's output is bounded by a small constant.

    Two checks run *before* the command-substitution veto, and the order is the whole
    point of them. The veto is a claim that a statement's output is unknowable because a
    substitution could print anything -- which is only true when the statement has a
    path to the terminal at all. A condition test has none, and a redirect has taken it
    away, so vetoing either is reasoning about output that cannot exist. Both shapes
    were blocked that way (`until [ "$(...)" = healthy ]`, `gh run view --log > file`)
    and neither could be spelled any other way.
    """
    peeled = strip_control_prefix(statement.strip())
    if NO_STDOUT_RE.match(peeled) or BARE_NO_OUTPUT_RE.match(peeled):
        return True
    if is_quiet_grep(peeled):
        return True
    # Quoted spans collapse to a word character rather than to a space, because a
    # redirect target is very often quoted (`--log > "/tmp/run.log"`) and blanking it
    # leaves a `>` with nothing after it to match. `q` keeps `> "x"` looking like a
    # redirect while still hiding a `>` that was only ever prose.
    if REDIRECT_RE.search(QUOTED_SPAN_RE.sub("q", peeled)):
        return True
    if SUBSTITUTION_RE.search(SINGLE_QUOTED_SPAN_RE.sub(" ", statement)):
        return False
    statement = peeled
    if GIT_LOG_RE.match(statement):
        flags = strip_quoted(statement)
        return bool(GIT_LOG_COUNT_RE.search(flags)) and not GIT_LOG_PATCH_RE.search(flags)
    if COMMIT_LIKE.match(statement):
        # Judged on the flags only: a `--dry-run` or `-v` anywhere in the *message* is
        # prose, and unbounding a commit because of what it says about itself is a false
        # positive with no visible cause.
        flags = strip_quoted(statement)
        return not (COMMIT_LIKE_DISQUALIFIERS.search(flags) or VERBOSE_FLAG_RE.search(flags))
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

    A cap anywhere in the pipeline counts, not just at the end: everything downstream
    of it can only ever receive what it passed, so `cat big | head -c 100 | grep x` is
    genuinely bounded and blocking it would be a false positive.
    """
    if WRAPPER_RE.search(statement):
        return True
    return any(CAP_RE.match(segment) for segment in split_top_level(statement, ("|",)))


def has_powershell_cap(statement: str) -> bool:
    """Whether a PowerShell pipeline contains a native global output bound."""
    if WRAPPER_RE.search(statement):
        return True
    segments = split_top_level(statement, ("|",))
    return any(
        POWERSHELL_SELECT_CAP_RE.match(segment.strip())
        or POWERSHELL_OUT_NULL_RE.match(segment.strip())
        for segment in segments
    )


def is_powershell_bounded(statement: str) -> bool:
    """True for PowerShell statements whose terminal output is constant-size."""
    statement = statement.strip()
    if POWERSHELL_ASSIGNMENT_RE.match(statement):
        # PowerShell assignment captures the RHS; it does not emit it to the pipeline.
        return True
    if POWERSHELL_SILENT_RE.match(statement) or POWERSHELL_FIXED_RE.match(statement):
        return True
    if POWERSHELL_CONTENT_CAP_RE.match(statement):
        return True
    if POWERSHELL_LENGTH_GUARD_RE.match(statement):
        return True
    if (
        POWERSHELL_GET_ITEM_RE.match(statement) or POWERSHELL_GET_ITEM_PROPERTY_RE.match(statement)
    ) and not re.search(r"[*?\[]", statement):
        # One result per authored literal path. Wildcards remain unbounded.
        return True
    return is_bounded(statement)


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


def is_capped(command: str, command_shell: str = "bash") -> bool:
    """True when EVERY statement in the command is capped or bounded.

    The `all` (rather than the `any` this once was) is the whole fix: a command is
    only as bounded as its least-bounded statement, and the old `re.search` over the
    joined string let `find / -name x; echo done | head -c 10` through.
    """
    parts = statements(command)
    if not parts:
        return False
    if command_shell == "powershell":
        return all(is_powershell_bounded(part) or has_powershell_cap(part) for part in parts)
    return all(is_bounded(part) or has_cap(part) for part in parts)


def decide(
    raw: str,
    max_bytes: int | None = None,
    command_shell: str = "bash",
) -> tuple[int, str]:
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

    if is_capped(command, command_shell=command_shell):
        return 0, ""

    return EXIT_BLOCK, block_message(cap, command_shell=command_shell)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", choices=("bash", "powershell"), default="bash")
    args = parser.parse_args(argv)
    exit_code, message = decide(sys.stdin.read(), command_shell=args.shell)
    if message:
        # stderr, not stdout: only stderr is surfaced for a blocking hook.
        print(message, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
