"""Unit tests for the enforce-capped-bash PreToolUse hook decision logic.

Vendored tier: `decide` takes an injectable `max_bytes` precisely so these tests do
not depend on the `[bash]` block of whichever repo they run in.
"""

import json
import re

import conftest
import pytest
from conftest import load_module

hook = load_module("scripts/hooks/enforce-capped-bash.py")


def payload(tool_name, command=None):
    body = {"tool_name": tool_name, "tool_input": {}}
    if command is not None:
        body["tool_input"]["command"] = command
    return json.dumps(body)


# --- decide: allow paths ---


def test_empty_stdin_allows():
    assert hook.decide("") == (0, "")
    assert hook.decide("   \n") == (0, "")


def test_malformed_json_allows_with_note():
    code, msg = hook.decide("{not json")
    assert code == 0
    assert "unable to parse" in msg


def test_non_bash_tool_allows_silently():
    assert hook.decide(payload("Read", "rm -rf /")) == (0, "")


def test_capped_with_invoke_wrapper_allows():
    cmd = 'python3 scripts/hooks/invoke-capped.py --command "ls" --max-bytes 4000'
    code, msg = hook.decide(payload("Bash", cmd))
    assert code == 0
    assert msg == ""


def test_capped_with_head_c_allows():
    code, _ = hook.decide(payload("Bash", "cat big.log | head -c 4000"))
    assert code == 0


def test_wrapper_without_explicit_cap_allows():
    """The wrapper's own default is the cap; a bare invocation is still capped."""
    cmd = 'python3 scripts/hooks/invoke-capped.py --command "ls"'
    code, _ = hook.decide(payload("Bash", cmd))
    assert code == 0


def test_an_unrecognised_wrapper_form_blocks():
    """Only the two documented forms pass. Anything that merely looks like a
    wrapper -- a different extension, a different interpreter -- must block, or
    the gate degrades into a substring coincidence."""
    cmd = 'pwsh -File scripts/hooks/invoke-capped.ps1 -Command "ls"'
    code, _ = hook.decide(payload("Bash", cmd))
    assert code == hook.EXIT_BLOCK


# --- decide: block paths ---


def test_uncapped_bash_blocks():
    code, msg = hook.decide(payload("Bash", "ls -la"))
    assert code == hook.EXIT_BLOCK
    assert "Blocked uncapped Bash command" in msg


def test_missing_command_blocks():
    code, msg = hook.decide(payload("Bash"))
    assert code == hook.EXIT_BLOCK
    assert "missing command text" in msg


def test_blank_command_blocks():
    code, msg = hook.decide(payload("Bash", "   "))
    assert code == hook.EXIT_BLOCK
    assert "missing command text" in msg


# --- Codex's Windows PowerShell port ----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content README.md | Select-Object -First 200",
        "Get-Content logs/test.log | Select-Object -Last 80",
        "Get-Content README.md -TotalCount 200",
        "Get-Content logs/test.log -Tail 80",
        "New-Item -ItemType Directory -Force .codex/tmp | Out-Null",
        "Start-Sleep -Seconds 2",
        "Test-Path scripts/hooks/invoke-capped.py",
        "Get-Item README.md",
        "(Get-Item -LiteralPath README.md).Length",
        (
            "$p='README.md'; $s=[IO.File]::ReadAllText($p); "
            "if($s.Length -gt 12000){$s.Substring(0,12000)}else{$s}"
        ),
    ],
)
def test_powershell_native_bounds_allow(command):
    assert hook.decide(payload("Bash", command), command_shell="powershell") == (0, "")


def test_powershell_wrapper_allows():
    command = (
        "python3 scripts/hooks/invoke-capped.py --shell powershell "
        "--command 'Get-ChildItem -Recurse'"
    )
    assert hook.decide(payload("Bash", command), command_shell="powershell") == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "Get-Content -Raw README.md",
        "Get-ChildItem -Recurse",
        "Get-Item *.md",
        "git status --short",
        "rg TODO .",
    ],
)
def test_uncapped_powershell_blocks(command):
    code, message = hook.decide(payload("Bash", command), command_shell="powershell")
    assert code == hook.EXIT_BLOCK
    assert "Blocked uncapped PowerShell command" in message
    assert "--shell powershell" in message


def test_one_powershell_cap_does_not_launder_a_second_statement():
    command = "Get-Content README.md | Select-Object -First 20; Get-ChildItem -Recurse"
    code, _ = hook.decide(payload("Bash", command), command_shell="powershell")
    assert code == hook.EXIT_BLOCK


def test_powershell_syntax_does_not_weaken_the_bash_policy():
    code, _ = hook.decide(payload("Bash", "cat README.md | Select-Object -First 20"))
    assert code == hook.EXIT_BLOCK


# --- alternate payload shapes ---


@pytest.mark.parametrize(
    "raw",
    [
        '{"toolName":"Bash","toolInput":{"command":"ls"}}',
        '{"tool":{"name":"Bash"},"input":{"command":"ls"}}',
        '{"name":"Bash","command":"ls"}',
    ],
)
def test_alternate_key_shapes_still_block_uncapped(raw):
    code, _ = hook.decide(raw)
    assert code == hook.EXIT_BLOCK


# --- the block message ---


def test_block_message_quotes_the_configured_cap():
    """The number in the message must be the number the wrapper will use.

    These drifted apart in the original: the message hard-coded 4000 while the
    wrapper's default came from elsewhere, so a project that widened the cap was
    told to pass a value it had deliberately changed.
    """
    _, msg = hook.decide(payload("Bash", "ls -la"), max_bytes=9999)
    assert "9999" in msg
    assert "4000" not in msg


def test_block_message_warns_about_the_shell():
    """The cmd.exe surprise is the most common way the wrapper bites a caller, so
    the block message -- not just the rule file -- has to say it."""
    _, msg = hook.decide(payload("Bash", "ls -la"))
    assert "cmd.exe" in msg
    assert "head -c" in msg


def test_block_message_defaults_to_the_manifest_value():
    _, msg = hook.decide(payload("Bash", "ls -la"))
    assert str(hook.CFG.bash.max_bytes) in msg


def test_block_message_states_the_minimum_cap():
    """The message named the default but not the floor, so the only way to find the
    floor was to trip it -- a wasted round-trip on a hook whose whole job is to save
    them. Asserted against the constant, not a literal, for the same reason the
    configured cap is."""
    _, msg = hook.decide(payload("Bash", "ls -la"))
    assert str(hook.harness_config.MIN_MAX_BYTES) in msg


def test_block_message_steers_test_runs_to_the_wrapper():
    """`head -c` is the message's headline escape hatch, but for pytest and ruff the
    signal is the trailing summary -- exactly what a head window discards. The message
    has to say which tool wins there, since it is what an agent reads in the moment."""
    _, msg = hook.decide(payload("Bash", "ls -la"))
    assert "tail" in msg
    assert "exit code" in msg


# --- is_capped / get_value units ---


def test_is_capped_true_and_false():
    assert hook.is_capped("foo | head -c 100") is True
    assert hook.is_capped("plain command") is False


# --- every statement needs its own cap ----------------------------------------
# The bypass this closes: `is_capped` was one `re.search` over the whole command, so a
# single capped segment laundered everything beside it.


@pytest.mark.parametrize(
    "command",
    [
        "find / -name x; echo done | head -c 10",
        "ls -R /tmp && cat log | head -c 10",
        "cat a | head -c 10; ls -R /",
        "ls -R / || cat b | head -c 10",
        "ls -R /\ncat b | head -c 10",
    ],
)
def test_one_capped_statement_does_not_launder_the_others(command):
    assert hook.is_capped(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_every_statement_capped_allows():
    assert hook.is_capped("cat a | head -c 10; cat b | head -c 10") is True


def test_a_capped_statement_beside_a_bounded_one_allows():
    """`cd x && git rev-parse HEAD` is entirely bounded; blocking it is a false positive."""
    assert hook.is_capped("cd /tmp && git rev-parse HEAD") is True


def test_statements_splits_on_every_separator():
    assert hook.statements("a; b && c || d\ne") == ["a", "b", "c", "d", "e"]


def test_statements_ignores_separators_inside_quotes():
    """Quote-awareness is what keeps a correctly-wrapped command from being blocked.

    `invoke-capped.py --command "cd x; make"` is one statement; splitting the quoted
    `;` would leave a bare `make"` that carries no cap of its own.
    """
    command = 'python3 scripts/hooks/invoke-capped.py --command "cd x; make test"'
    assert hook.statements(command) == [command]
    assert hook.is_capped(command) is True


def test_statements_handles_escaped_quotes():
    assert hook.statements(r'echo "a \" b"; pwd') == [r'echo "a \" b"', "pwd"]


def test_ampersand_alone_is_not_a_separator():
    """Backgrounding does not bound output, so `a & b` must not read as two statements."""
    assert hook.statements("sleep 1 & ls -R /") == ["sleep 1 & ls -R /"]


def test_a_cap_anywhere_in_a_pipeline_counts():
    """Everything downstream of `head -c N` can only receive N bytes."""
    assert hook.has_cap("cat big | head -c 100 | grep x") is True
    assert hook.has_cap("cat big | grep x") is False


def test_empty_command_is_not_capped():
    assert hook.is_capped("") is False
    assert hook.is_capped("   ;  ") is False


# --- bounded-by-construction commands are exempt ------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "whoami",
        "hostname",
        "date +%s",
        "echo hello world",
        # Same reasoning as echo, and found by the gate blocking a real `printf ... &&`
        # chain: the text printed is text the command already spent context on.
        "printf 'done: %s\\n' ok",
        "cd /tmp",
        "mkdir -p a/b/c",
        "touch x.py",
        "which python",
        "command -v gh",
        "git rev-parse --show-toplevel",
        "git rev-parse HEAD",
        "git branch --show-current",
        "git symbolic-ref --quiet refs/remotes/origin/HEAD",
        "git describe --tags",
        "git rev-list --count HEAD",
        "git config --get commit.gpgsign",
        "python --version",
        "node -V",
        # Condition tests print nothing at all, under any flag.
        "test -f pyproject.toml",
        "test -d .venv",
        "[ -f pyproject.toml ]",
        "[[ -n $DEVKIT_DIR ]]",
    ],
)
def test_bounded_commands_need_no_wrapper(command):
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # The criterion is bounded *regardless of tree size*, which these are not --
        # they are also the commands most often blocked, and the intended answer for
        # them is the Read/Glob/Grep tools rather than an exemption here.
        "ls",
        "ls -R /",
        "cat setup.py",
        "git status",
        "git status --porcelain",
        "git diff --stat",
        "git log --oneline",
        "find . -name '*.py'",
        "grep -r foo .",
        # Long output, despite looking like a version probe.
        "python --help",
    ],
)
def test_unbounded_commands_are_not_exempt(command):
    assert hook.is_bounded(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_command_substitution_voids_a_bounded_claim():
    """`echo $(find / -name x)` prints whatever the substitution found."""
    assert hook.is_bounded("echo $(find / -name x)") is False
    assert hook.is_bounded("echo `find / -name x`") is False
    assert hook.decide(payload("Bash", "echo $(ls -R /)"))[0] == hook.EXIT_BLOCK


def test_an_existence_probe_is_allowed():
    """`test -f x && echo yes || echo no` is the idiom the gate used to make unspellable.

    Every word in it was already exempt except the one that can never print, so the
    block had no legal remedy: the wrapper caps output that does not exist, and there
    is no other spelling of "does this file exist" without a `test`.
    """
    command = "test -f .devkit.toml && echo EXISTS || echo MISSING"
    assert hook.is_capped(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


def test_a_condition_test_does_not_launder_an_unbounded_statement():
    """The exemption is per-statement, so the `ls` still decides."""
    assert hook.is_capped("test -d src && ls -R src") is False


def test_a_condition_test_bounds_a_substitution_it_consumes():
    """This assertion used to read `is False`, and reversing it was deliberate.

    The reasoning behind the old expectation was the general one for the substitution
    veto: `$(...)` can print anything, so a statement containing one cannot be called
    bounded. That holds wherever the statement has a path to the terminal -- and a
    condition test has none. `test` consumes the substitution's output as an *argument*
    and writes nothing to stdout under any flag, so the bytes never reach the agent.

    The veto was therefore refusing on output that cannot exist, and it did so on the
    shape every readiness poll in this workspace is written in
    (`until [ "$(docker inspect ...)" = healthy ]`), where the remedy the block message
    offers -- cap it -- caps an empty stream. This gate budgets context, not risk; what
    the substitution *does* is not its question.
    """
    assert hook.is_bounded("test -n $(ls -R /)") is True
    # The distinction is whether the statement can print, not whether it is a builtin:
    # `echo` puts the same substitution straight into context.
    assert hook.is_bounded("echo $(ls -R /)") is False


def test_a_bounded_prefix_does_not_exempt_a_longer_word():
    """`date` is bounded; `dates_report.sh` is a different command entirely."""
    assert hook.is_bounded("dateutil-dump --all") is False
    assert hook.is_bounded("echoes-everything") is False
    assert hook.is_bounded("testrunner --all") is False


def test_block_message_explains_both_new_rules():
    _, msg = hook.decide(payload("Bash", "ls -R /"))
    assert "Every statement needs its own cap" in msg
    # An agent that does not know ls is excluded on purpose will keep trying to wrap it.
    assert "ls/cat/git status" in msg


# --- three shapes the gate was never meant to catch ---------------------------
# Each of these blocked a real call in one session, and each was worse than an ordinary
# false positive: the remedy the block message offers does not resolve any of them.


@pytest.mark.parametrize(
    "command",
    [
        # The exact chain that was blocked: everything is silent or capped, and the
        # `rm` alone stopped it. There is no way to cap a command that prints nothing.
        'cd /tmp && rm -rf out && mkdir -p out && python3 scripts/hooks/invoke-capped.py --command "x"',
        "rm -rf build",
        "rm -f a.txt b.txt",
        "cp a b",
        "mv a b",
        "rmdir empty",
        "ln -s a b",
        "chmod +x scripts/hook.py",
    ],
)
def test_commands_silent_on_success_need_no_wrapper(command):
    assert hook.is_capped(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # `-v` is the one flag that turns this family into per-file output scaling with
        # the tree, and it was a live hole in the entries that were already exempt.
        "rm -rv big/",
        "mkdir -pv a/b/c",
        "cp -rv src dst",
        "chmod -R --verbose 755 .",
    ],
)
def test_verbose_revokes_the_silent_on_success_exemption(command):
    assert hook.is_bounded(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_a_loop_whose_body_is_capped_is_allowed():
    """Before this, EVERY loop was blocked, whatever its body did.

    `statements()` splits on `;`, so a loop arrives shredded into `do` / `done`
    fragments that can carry no cap and match no bounded command. The block message's
    remedy cannot help either: the wrapper runs through cmd.exe, where bash loop syntax
    is a parse error, so this shape had no legal spelling at all.
    """
    command = (
        'for f in *.py; do python3 scripts/hooks/invoke-capped.py --command "ruff check $f"; done'
    )
    assert hook.is_capped(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


def test_a_loop_body_still_needs_its_cap():
    """The keyword is peeled off; what it introduces is judged on its own merits."""
    assert hook.is_bounded("do ls -R /") is False
    assert hook.is_capped('for d in */; do ls -R "$d"; done') is False
    assert hook.decide(payload("Bash", "for d in */; do ls -R $d; done"))[0] == hook.EXIT_BLOCK


@pytest.mark.parametrize(
    "fragment",
    ["done", "fi", "esac", "then", "else", "}", "for f in *.py", "case $x in", "do rm -rf x"],
)
def test_control_flow_fragments_are_bounded(fragment):
    assert hook.is_bounded(fragment) is True


# --- the four loop shapes the control-flow fix stopped one spelling short of ----
#
# Reported as "the gate blocked a bare `until [ -f … ]; do sleep 3; done`, which its own
# block message lists as exempt". That exact spelling passed; each of these neighbours of
# it did not, and every one is the same defect -- a fragment that emits nothing, blocked
# with a remedy (wrap it) that cannot be applied to a loop at all.


@pytest.mark.parametrize(
    "command",
    [
        # `!` is a control keyword like any other: it inverts an exit status and prints
        # nothing. The peel loop stopped at it, so the condition behind it was never
        # judged and the whole loop blocked on one character.
        "until ! [ -f logs/run.log ]; do sleep 3; done",
        "while ! test -f logs/run.log; do sleep 3; done",
        "if ! [ -f .venv ]; then mkdir .venv; fi",
        # A comment is not a command. It also runs to end of line, so it swallows the
        # `;` inside it rather than yielding a second statement.
        "until [ -f logs/run.log ]; do sleep 3; done  # wait for the runner",
        "pwd  # where am I",
        # A loop's redirections attach to its `done`. Neither `<` nor `2>` bounds
        # anything -- they are not stdout -- but neither emits anything either.
        "until [ -f logs/run.log ]; do sleep 3; done < /dev/null",
        "until [ -f logs/run.log ]; do sleep 3; done 2>/dev/null",
        "while read -r line; do echo $line; done < paths.txt",
        # `;;` contains the `;` the splitter cuts on, so an arm arrives as a header and
        # a pattern in front of its command.
        "case $x in a) pwd ;; esac",
        "case $mode in a|b) rm -f out ;; *) sleep 1 ;; esac",
    ],
)
def test_the_loop_shapes_beside_the_one_that_was_fixed(command):
    assert hook.decide(payload("Bash", command)) == (0, "")


# --- a loop header whose word list is a command substitution --------------------
#
# The next spelling along, and it was blocked for a reason the module docstring already
# ruled out: the substitution veto fired on `$(seq 1 60)`. `is_bounded` ran the
# control-flow check *after* that veto, so the exemption it was supposed to grant could
# never be reached by any header containing a `$(` or a backtick.
#
# What makes it wrong is that the substitution is not the thing being run. `seq`'s output
# is the loop's word list -- the shell consumes it to decide how many iterations there are
# and prints not one byte of it, exactly as a condition test consumes the substitution
# feeding it. `until [ "$(docker inspect ...)" = healthy ]` had already been moved above
# the veto for precisely that reason; the loop header is the same argument.
#
# Reported from a session that needed to poll an API until it recovered, found the two
# obvious spellings split -- `while true; do ...; done` allowed, `for i in $(seq 1 60);
# do ...; done` blocked -- and could not wrap either, because the wrapper runs through
# cmd.exe where bash loop syntax is a parse error.


@pytest.mark.parametrize(
    "command",
    [
        # The reported case: a bounded retry loop.
        "for i in $(seq 1 60); do sleep 30; done",
        "for i in `seq 1 60`; do sleep 30; done",
        # The word list is a repository query, which is the whole reason to use one.
        'for f in $(git ls-files "*.py"); do python3 scripts/hooks/invoke-capped.py '
        '--command "ruff check $f"; done',
        # A substitution in the redirect a loop's `done` carries: it names a file, and a
        # file name is not output either.
        "while read -r line; do sleep 1; done < $(mktemp)",
        # `case` selects on one too.
        "case $(uname) in Linux) pwd ;; esac",
    ],
)
def test_a_substitution_in_a_control_header_is_not_output(command):
    assert hook.decide(payload("Bash", command)) == (0, "")


def test_the_header_exemption_does_not_reach_the_loop_body():
    """The guard against fixing this too widely.

    Only the *header* consumes the substitution. Whatever the body prints still reaches
    the terminal and is still judged on its own, so this stays blocked -- on the `cat`,
    which is where the block belongs and what the author can act on.
    """
    command = "for f in $(ls); do cat $f; done"
    code, message = hook.decide(payload("Bash", command))
    assert code != 0, "a loop body's output is not covered by its header"
    assert "cat" in message


def test_the_substitution_veto_still_applies_to_a_command_that_prints():
    """The veto is not weakened, only reordered: `echo` does have a path to the terminal,
    so what a substitution hands it is genuinely unknowable."""
    assert hook.is_bounded("echo $(find / -name x)") is False


@pytest.mark.parametrize(
    "fragment",
    ["for i in $(seq 1 60)", "for f in `git ls-files`", "case $(uname) in", "done < $(mktemp)"],
)
def test_control_headers_are_bounded_even_with_a_substitution(fragment):
    assert hook.is_bounded(fragment) is True


def test_a_loop_over_a_substitution_is_judged_on_its_body():
    """The fifth neighbour: a header whose word list comes from a substitution.

    `for f in $(git diff --name-only)` was vetoed as unknowable output before the
    control-flow check ever ran, though the substitution feeds the loop variable and
    never the terminal -- the same reasoning-about-output-that-cannot-exist that had
    already moved the condition tests ahead of the veto. The body still arrives as its
    own statements and is judged on its own merits.
    """
    assert hook.is_bounded("for f in $(git diff --name-only)") is True
    assert hook.is_bounded("case $(uname -s) in") is True
    command = "for f in $(git diff --name-only); do echo $f; done"
    assert hook.decide(payload("Bash", command)) == (0, "")
    # The exemption is the header's alone: an unbounded body still blocks the loop...
    blocked = "for f in $(git diff --name-only); do cat $f; done"
    assert hook.decide(payload("Bash", blocked))[0] == hook.EXIT_BLOCK
    # ...and a substitution with a real path to the terminal is still vetoed.
    assert hook.is_bounded("echo $(find / -name x)") is False


def test_an_env_assignment_prefix_does_not_revoke_an_exemption():
    """`DEVKIT_SKIP_BRANCH_POLICY=1 git commit -F m` is the branch policy's own
    documented bypass, and this gate blocked it: the assignment prefix broke the
    COMMIT_LIKE match, so the one spelling another gate's error message tells the
    agent to type was refused with a remedy (wrap it) that destroys a commit. The
    prefix emits nothing; the command behind it decides.
    """
    assert hook.is_bounded("DEVKIT_SKIP_BRANCH_POLICY=1 git commit -F msg.txt") is True
    assert hook.is_bounded("GIT_PAGER=cat git rev-parse HEAD") is True
    assert hook.is_bounded('CFLAGS="-O2 -g" make install > build.log') is True
    # The exemptions stay intact behind the prefix, in both directions.
    assert hook.is_bounded("FOO=bar ls -R /") is False
    assert hook.decide(payload("Bash", "RUST_LOG=debug cargo test"))[0] == hook.EXIT_BLOCK
    assert hook.decide(payload("Bash", "GIT_EDITOR=true git commit -F msg.txt")) == (0, "")


def test_a_comment_swallows_the_separator_inside_it():
    """`pwd # a; ls -R /` is one statement to a shell, and must be one here.

    Stripping comments per-statement instead would read the `ls -R /` as real, block a
    command the shell will never run, and -- worse in the other direction -- let the
    text after a `#` decide anything at all.
    """
    assert hook.statements("pwd # a; ls -R /") == ["pwd"]
    assert hook.decide(payload("Bash", "pwd # a; ls -R /")) == (0, "")


def test_a_hash_inside_a_word_is_not_a_comment():
    """Only a `#` that starts a word opens one; the rest are ordinary argument text."""
    assert hook.statements("cp logs/x#2.log out/") == ["cp logs/x#2.log out/"]
    assert hook.is_bounded("cp logs/x#2.log out/") is True


def test_a_command_that_is_only_a_comment_still_blocks():
    """The one shape left blocked, deliberately: it parses to no statements at all, and
    `is_capped` treats an empty parse as uncapped so a payload whose command vanishes is
    never allowed on the strength of having vanished. A pure comment runs nothing, so
    the block costs nothing real.
    """
    assert hook.statements("# just a note") == []
    assert hook.decide(payload("Bash", "# just a note"))[0] == hook.EXIT_BLOCK


@pytest.mark.parametrize(
    "command",
    [
        # Shell bookkeeping: changes an option, arms a handler, signals a pid, consumes
        # a line. `set -euo pipefail` heads a poll loop as often as the keyword does.
        "set -euo pipefail",
        "set -e",
        "shopt -s nullglob",
        "trap 'rm -f /tmp/lock' EXIT",
        "kill 4213",
        "read -r line",
        # Legal argument-less spellings, equally silent.
        "cd",
        "wait",
        ":",
    ],
)
def test_shell_bookkeeping_needs_no_wrapper(command):
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


def test_bare_set_is_still_blocked():
    """The whole reason the bookkeeping family keeps its trailing `\\s`.

    A lone `set` prints every shell variable and function -- output that scales with the
    environment, which is the one thing this gate exists to stop. Relaxing the argument
    requirement to admit bare `cd` would have admitted this too, which is why the
    argument-less spellings are a separate, enumerated pattern.
    """
    assert hook.is_bounded("set") is False
    assert hook.decide(payload("Bash", "set"))[0] == hook.EXIT_BLOCK


@pytest.mark.parametrize(
    "command",
    [
        # The other half of a readiness poll: once the thing being waited for is a line
        # in a file rather than the file itself, `[ -f x ]` becomes `grep -q`.
        "until grep -q 'Listening on' logs/run.log; do sleep 3; done",
        "grep -q TODO README.md",
        "grep -rq secret .",
        "grep --quiet TODO README.md",
        # A quiet last stage consumes the pipeline exactly as a `head -c` cap does.
        "docker ps --format '{{.Names}}' | grep -q db-1",
        "until ! docker ps --format '{{.Names}}' | grep -q db-1; do sleep 2; done",
    ],
)
def test_a_quiet_grep_is_bounded(command):
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # `-q` is a flag, not any `q`: a bare `q` is the pattern being searched for.
        "grep -r q .",
        "grep 'x -q y' big.log",
        # Quiet, but not last: what follows it prints.
        "cat big | grep -q x | cat",
        # The loop keyword is peeled, and what it introduces is judged as always.
        "until ! ls -R /; do sleep 3; done",
        "case $x in a) ls -R / ;; esac",
        "while read -r line; do cat $line; done < paths.txt",
        # A comment cannot launder the statement in front of it.
        "ls -R /  # just looking",
    ],
)
def test_the_loop_relaxations_do_not_reach_what_the_gate_is_for(command):
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_a_paren_in_a_commit_message_is_not_a_case_arm():
    """The case-arm peel must not be triggerable by prose.

    `[^()&|;'"]+\\)` would otherwise match through a message: `git commit -m "fix x)
    here"` peels to `here"` and blocks the commit the gate explicitly exempts. Excluding
    quote characters from the pattern is what stops it, and this is the regression.
    """
    command = 'git commit -m "fix x) here"'
    assert hook.strip_control_prefix(command) == command
    assert hook.decide(payload("Bash", command)) == (0, "")


def test_a_heredoc_body_is_not_read_as_statements():
    """A commit message is data. The newline split read every line of one as a command.

    Nothing about the body can be capped, and a heredoc cannot be handed to the wrapper
    either (it does not survive cmd.exe) — so this shape forced a write-the-message-to-a-
    file detour every single time.
    """
    command = "git commit -F - <<'EOF' | head -c 400\nSubject line\n\nls -R / in the body\nEOF"
    assert hook.statements(command) == ["git commit -F - <<'EOF' | head -c 400"]
    assert hook.is_capped(command) is True


def test_a_heredoc_does_not_launder_the_statements_after_it():
    command = "cat <<EOF | head -c 100\nbody\nEOF\nls -R /"
    assert hook.statements(command) == ["cat <<EOF | head -c 100", "ls -R /"]
    assert hook.is_capped(command) is False


@pytest.mark.parametrize("operator", ["<<EOF", "<<-EOF", "<<'EOF'", '<<"EOF"'])
def test_every_heredoc_operator_spelling_consumes_its_body(operator):
    command = f"cat {operator} | head -c 100\nls -R /\nEOF"
    assert hook.statements(command) == [f"cat {operator} | head -c 100"]


def test_an_unterminated_heredoc_consumes_the_rest():
    """What a shell does too. Failing closed here would block on a typo instead."""
    assert hook.statements("cat <<EOF | head -c 10\nbody\nmore") == ["cat <<EOF | head -c 10"]


def test_a_here_string_is_not_a_heredoc():
    """`<<<` feeds one word, not a body; treating it as one would swallow real code."""
    assert hook.statements("grep x <<<'text'\nls -R /") == ["grep x <<<'text'", "ls -R /"]


def test_a_heredoc_inside_quotes_is_not_an_operator():
    command = "python3 scripts/hooks/invoke-capped.py --command \"echo 'a << b'\""
    assert hook.statements(command) == [command]


# --- git add is silent on success, like rm and cp ---


@pytest.mark.parametrize("command", ["git add -A", "git add -- .", "git add path/to/file"])
def test_git_add_is_bounded(command):
    """No output on success means no output to cap, so blocking it has no remedy."""
    assert hook.is_bounded(command) is True


def test_git_add_before_a_heredoc_commit_is_allowed():
    """The shape this was found in: the staging step blocked the whole commit."""
    command = "git add -A && git commit -F - <<'EOF' | head -c 500\nSubject\nEOF"
    assert hook.is_capped(command) is True


def test_verbose_git_add_is_not_bounded():
    """`-v` prints a line per file, so it scales with the change like any other."""
    assert hook.is_bounded("git add -v -A") is False


@pytest.mark.parametrize("command", ["git added -A", "git address"])
def test_the_git_add_exemption_does_not_extend_by_prefix(command):
    """`\\s` after the alternative is what stops `git add` matching a longer word."""
    assert hook.is_bounded(command) is False


def test_other_git_subcommands_stay_blocked():
    """The exemption is `git add` specifically, not `git`."""
    assert hook.is_bounded("git status") is False
    assert hook.is_bounded("git diff") is False
    assert hook.is_bounded("git log") is False


# --- the commit itself, which the `git add` fix stopped one command short of ---
#
# Exempting the staging step and leaving `git commit` blocked meant every commit still
# cost a block message -- the most repeated Bash call in the harness. There is no legal
# spelling to fall back on: the message is multi-line, so the wrapper's cmd.exe mangles
# it, and `| head -c N` masks the exit code, which on a commit means a pre-commit
# rejection reports success.


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Subject line"',
        'git commit -am "Subject line"',
        "git commit --amend --no-edit",
        # A message spanning newlines: the shape that is reported, and the one no
        # wrapper can carry.
        'git commit -m "Subject\n\nBody paragraph."',
        # PowerShell's here-string, which is what the Bash tool receives on Windows.
        "git commit -m @'\nSubject\n\nBody paragraph.\n'@",
        # The single URL `gh pr create` prints is bounded by nothing at all, and its
        # --body has the same multi-line problem as a commit message.
        'gh pr create --title "t" --body "line one\n\nline two"',
    ],
)
def test_a_commit_with_a_message_is_bounded(command):
    assert hook.is_bounded(command) is True
    assert hook.is_capped(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git -C /repo add -A",
        'git -C "C:/path with spaces/repo" add -A',
        "git -C /repo commit -F msg.txt",
        "git --no-pager -C /repo commit -F msg.txt",
        "git -c user.name=agent commit -F msg.txt",
    ],
)
def test_git_global_options_do_not_revoke_the_commit_pair_exemption(command):
    """`git -C <box> add` is the workspace's own spelling: the box is never the cwd.

    The exemption used to require the subcommand to follow `git` immediately, so every
    commit made into an ephemeral box was blocked -- while the block message went on
    promising the commit pair was exempt.
    """
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # The disqualifiers `search` the whole statement, so they still reach a flag
        # written after a global option.
        "git -C /repo commit --dry-run",
        "git -C /repo commit -v",
        "git -C /repo add -v .",
        # Not the commit pair at all; `-C` must not launder an unbounded subcommand.
        "git -C /repo log --oneline",
        "git -C /repo status",
    ],
)
def test_a_global_option_does_not_launder_an_unbounded_git_command(command):
    assert hook.is_bounded(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_the_reported_commit_shape_is_one_statement():
    """The newline split must not read the message body as uncappable statements."""
    command = "git commit -m @'\nCut CLAUDE.md to what only it can say\n\nBody.\n'@"
    assert hook.statements(command) == [command]


def test_a_heredoc_commit_needs_no_head_cap_now():
    """The spelling the suite used to require -- `head -c` masks the commit's exit code."""
    assert hook.is_capped("git add -A && git commit -F - <<'EOF'\nSubject\nEOF") is True


@pytest.mark.parametrize("flag", ["--dry-run", "--short", "--porcelain", "--long"])
def test_a_dry_run_commit_is_not_bounded(flag):
    """`git commit --dry-run` is `git status` renamed: it lists every untracked path."""
    assert hook.is_bounded(f"git commit {flag}") is False


@pytest.mark.parametrize("command", ["git commit -v", "git commit --verbose", "git commit -av"])
def test_a_verbose_commit_is_not_bounded(command):
    """`-v` appends the full staged diff, which scales with the change without limit."""
    assert hook.is_bounded(command) is False


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "Add a --verbose flag to the runner"',
        'git commit -m "Support --dry-run"',
        "git commit -m @'\nStop -v from unbounding a commit\n'@",
        'gh pr create --body "Adds --dry-run and -v"',
    ],
)
def test_a_flag_named_in_the_message_does_not_unbound_the_commit(command):
    """Prose is not flags.

    Reading the disqualifiers off the raw statement makes a commit's own subject decide
    whether it is allowed -- a false positive with no cause the block message can name,
    firing on exactly the commits that describe this hook.
    """
    assert hook.is_bounded(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m 'Fix `foo` in the runner'",
        "git commit -m @'\nStop `SUBSTITUTION_RE` firing on prose\n'@",
        "git commit -m 'Handle $(x) in messages'",
    ],
)
def test_a_backtick_inside_single_quotes_is_prose_not_substitution(command):
    """The shell would not expand it there, so neither does this gate.

    Found by this hook blocking the commit that introduced the exemption above: a
    message about code is full of backticked identifiers, and every one of them read as
    command substitution. The block message names no cause the author can act on.
    """
    assert hook.is_bounded(command) is True


@pytest.mark.parametrize(
    "command",
    ['git commit -m "Fix `foo`"', 'git commit -m "Fix $(id) now"', "echo $(find / -name x)"],
)
def test_substitution_inside_double_quotes_still_unbounds(command):
    """Double quotes expand it, so the output claim really is void."""
    assert hook.is_bounded(command) is False


def test_strip_quoted_blanks_both_quote_styles_across_newlines():
    assert "keep" in hook.strip_quoted("keep 'drop --dry-run' more")
    assert "--dry-run" not in hook.strip_quoted("git commit -m 'a\n--dry-run\nb'")
    assert "--dry-run" not in hook.strip_quoted('git commit -m "a\n--dry-run\nb"')


@pytest.mark.parametrize("command", ["git commit-tree x", "git committed", "gh pr createx"])
def test_the_commit_exemption_does_not_extend_by_prefix(command):
    assert hook.is_bounded(command) is False


def test_other_gh_pr_subcommands_stay_blocked():
    """`gh pr list`/`view` scale with the repo, and both wrap without trouble.

    `view` is the sharpest line here: it is the one `gh pr` subcommand that *prints* a
    body instead of supplying one, so the very thing that exempts `edit` is what keeps
    `view` blocked.
    """
    assert hook.is_bounded("gh pr list") is False
    assert hook.is_bounded("gh pr view --json number,url,state") is False


@pytest.mark.parametrize(
    "command",
    [
        # Found by correcting a PR body this gate's own change had just written: same
        # authored `--body-file`, same single URL back, exempt only for `create`.
        'gh pr edit 100 --body-file "C:/tmp/body.md"',
        "gh pr comment 100 --body-file body.md",
        "gh pr edit 100 --title 'Stop blocking the loops it calls exempt'",
    ],
)
def test_the_other_authored_gh_pr_commands_are_bounded(command):
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


# --- the /ship skill has to scope this gate to Claude Code ---
#
# The gate blocks every one of /ship's five steps -- `ship.py --preflight`, `git
# status`, `git diff`, `ship.py`, `gh pr view` -- because each one's output scales
# with the repo in Claude Code. Codex already bounds shell output, so importing the
# same policy caused a blocked call and a wrapper retry instead. These assert both
# agent-specific directives remain discoverable and project-agnostic.

SHIP_SKILL = conftest.REPO_ROOT / ".claude/skills/ship/SKILL.md"
BASELINE_RULE = conftest.REPO_ROOT / ".claude/rules/engineering.md"
WRAPPER_RELPATH = "scripts/hooks/invoke-capped.py"


def test_the_baseline_rule_introduces_the_gate():
    """The gate has to be discoverable somewhere other than a block message.

    `engineering.md` is unscoped and vendored byte-identical, so it is the one file
    that reaches every task in every project. Before it named the wrapper, being
    blocked was the *only* way to learn the hook existed -- which is why the block
    message grew to a ~1 KB tutorial repeated on every hit.
    """
    text = BASELINE_RULE.read_text(encoding="utf-8")
    assert WRAPPER_RELPATH in text
    assert "enforce-capped-bash.py" in text
    assert "Codex is the exception" in text
    assert "Codex shell commands directly" in text


def test_the_ship_skill_distinguishes_claude_from_codex():
    """Claude needs the wrapper; prescribing it to Codex recreates the noisy retry."""
    text = SHIP_SKILL.read_text(encoding="utf-8")
    assert WRAPPER_RELPATH in text
    assert "Codex runs the numbered commands directly" in text


def test_the_wrapper_the_ship_skill_names_exists():
    """A directive naming a path that moved is worse than none: it reads as verified."""
    assert (conftest.REPO_ROOT / WRAPPER_RELPATH).is_file()


def test_the_ship_skill_does_not_pin_a_byte_cap():
    """`--max-bytes` defaults to this project's `[bash] max_bytes`.

    SKILL.md is vendored byte-for-byte, so a literal here is one project's cap
    imposed on every other -- the exact hard-coding `CLAUDE.md` forbids in a vendored
    file. Leaving the flag off is what makes the one wording correct everywhere.
    """
    text = SHIP_SKILL.read_text(encoding="utf-8")
    assert not re.search(r"--max-bytes[= ]\s*\d", text)


def test_a_backticked_body_in_double_quotes_is_not_bounded():
    """The gate is right about this one, which is what makes the skill the fix.

    `$(...)` and backticks expand inside double quotes, so the shell really would run
    them -- `is_bounded` refuses the statement and must keep refusing it. What made it
    expensive is that the block message names the missing cap, so the backtick is
    invisible as the cause and the wrapper it recommends does not help.
    """
    inline = 'gh pr create --title "t" --body "it calls `known_projects` first"'
    assert not hook.is_bounded(inline)
    assert hook.is_bounded('gh pr create --title "t" --body-file body.md')
    # Single quotes are the one-liner escape: the shell does not expand there.
    assert hook.is_bounded("git commit -m 'fix `known_projects`'")


def test_the_ship_skill_says_how_to_pass_a_backticked_message():
    """A commit subject or PR body about this codebase names identifiers, and a Markdown
    body backticks them -- so `-m "..."` and `--body "..."` fail on exactly the messages
    worth writing. The skill exempts both commands from the wrapper and used to stop
    there, which left the author to rediscover the quoting rule from a block message
    that names the cap instead of the backtick.
    """
    text = SHIP_SKILL.read_text(encoding="utf-8")
    assert "--body-file" in text
    assert "git commit -F" in text


# --- the cap is a family, not one spelling -------------------------------------
# Measured, not guessed: over the workspace's transcripts a little over half of every
# block this hook had ever issued was one of the shapes below. Each was blocked by a
# gate whose own message recommends piping into head/tail.


@pytest.mark.parametrize(
    "command",
    [
        # The self-contradiction that made this worth measuring: the block message says
        # to keep the tail on a test run because the summary is the part you need.
        "python -m pytest scripts/hooks/tests/ -q 2>&1 | tail -c 2500",
        "gh run view --job 91847812208 --log-failed 2>&1 | tail -c 4000",
        "cat big.log | tail -c 512",
    ],
)
def test_tail_c_is_a_cap_exactly_as_head_c_is(command):
    assert hook.has_cap(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        "git ls-files | head -100",
        "git ls-files | head -n 100",
        "gh pr view 116 --json number,title,body 2>&1 | head -60",
        "cat big.log | tail -20",
        "cat big.log | tail -n 20",
        # A file argument rather than a pipe: still bounded to N lines.
        "head -50 big.log",
    ],
)
def test_a_line_count_is_a_cap(command):
    """Weaker than a byte cap and admitted deliberately -- see `CAP_RE`.

    A line count is bounded regardless of repo or filesystem size, which is this
    hook's stated criterion, and it is the bound the Read tool applies too. What it
    does not bound is line length.
    """
    assert hook.has_cap(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # "From line 5 to the end" is not a bound at all.
        "cat big.log | tail -n +5",
        # Does not terminate, so it bounds nothing and hangs the turn.
        "tail -f app.log",
        # No count anywhere.
        "cat big.log | head",
    ],
)
def test_a_head_or_tail_that_bounds_nothing_is_not_a_cap(command):
    assert hook.has_cap(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


# --- redirected output never reaches the agent ---------------------------------


@pytest.mark.parametrize(
    "command",
    [
        'gh run view 31437747160 --job 93615604936 --log > "/tmp/run.log"',
        "ls -R / > /dev/null",
        "find . -name '*.py' >> inventory.txt",
        "cat huge.log 1> out.txt",
        "make build &> build.log",
    ],
)
def test_stdout_redirected_to_a_file_is_bounded(command):
    """The strongest bound there is: the output is not in the agent's context at all.

    The remedy the block message offered for these -- cap the output -- caps a stream
    that was never going to arrive.
    """
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # A file-descriptor duplication, not a redirect of stdout to a file. Reading it
        # as one would exempt most of the commands this gate exists to catch, since
        # `2>&1` is how they are all written.
        "ls -R / 2>&1",
        "grep -r foo . 2>&1",
        "cat big.log >&2",
        # stderr only: stdout still reaches the terminal. The no-space spelling is the
        # common one and the likeliest regression -- `2>/dev/null` is a hair away from
        # reading as a redirect and would exempt most of what this gate is for.
        "ls -R / 2>/dev/null",
        "grep -r foo . 2>/dev/null",
        "ls -R / 2> errors.txt",
    ],
)
def test_a_descriptor_dup_is_not_a_redirect(command):
    assert hook.is_bounded(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_a_redirect_inside_quotes_does_not_bound():
    """Prose is not a redirect: the `>` here is inside the message."""
    assert hook.is_bounded('grep -r "a > b" .') is False


# --- the substitution veto must not outrank a statement with no stdout ---------


def test_a_substitution_inside_a_condition_stays_bounded():
    """`until [ "$(docker inspect ...)" = healthy ]` is how every readiness poll here
    is written, and the veto blocked all of them. `[` has no stdout path, so the
    substitution feeds the condition and never the terminal.
    """
    poll = 'until [ "$(docker inspect --format \'{{.State.Status}}\' db-1)" = "running" ]'
    assert hook.is_bounded(poll) is True


def test_a_substitution_whose_output_is_redirected_stays_bounded():
    assert hook.is_bounded("echo $(ls -R /) > /dev/null") is True


def test_the_veto_still_holds_where_output_can_reach_the_terminal():
    """The ordering change must not weaken the rule it reorders."""
    assert hook.is_bounded("echo $(find / -name x)") is False
    assert hook.is_bounded("echo `ls -R /`") is False


def test_the_readiness_poll_that_was_blocked_end_to_end():
    """The exact shape from the transcripts, every fragment of it."""
    command = (
        'until [ "$(docker inspect --format \'{{.State.Health.Status}}\' db-1)" = "healthy" ];'
        ' do sleep 2; done; echo "db healthy"'
    )
    assert hook.decide(payload("Bash", command)) == (0, "")


# --- git log is bounded exactly when it is told how many commits to print ------


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -3",
        "git log --oneline -25",
        "git log -n 5 --format=%H",
        "git log --max-count=10",
        "git log --max-count 10",
        'git -C "C:/Users/x/devkit" log --oneline -5',
    ],
)
def test_a_counted_git_log_is_bounded(command):
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


@pytest.mark.parametrize(
    "command",
    [
        # Scales with history: the case the exemption must not reach.
        "git log",
        "git log --oneline",
        "git log --format=%H",
        # One commit's diff has no bound, so a count does not earn the exemption.
        "git log -p -3",
        "git log --patch -1",
        "git log -3 -u",
    ],
)
def test_an_uncounted_or_patch_git_log_stays_blocked(command):
    assert hook.is_bounded(command) is False
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


# --- the remaining single-command gaps -----------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Found by the poll loop above: once the control keywords became bounded, the
        # `sleep` was the last fragment in the line still able to block it.
        "sleep 2",
        "sleep 10",
        # Move a checkout, answer with a fixed confirmation or nothing.
        "git checkout -- .secrets.baseline scripts/hooks/tests/test_repo_contract.py",
        "git checkout -b agent/topic-0813",
        "git switch main",
        "git restore --staged pyproject.toml",
        # One line: a URL, a sha, or nothing at all.
        "git remote get-url origin",
        "git merge-base --is-ancestor origin/agent/x origin/main",
        # Silent on success, one diagnostic on failure.
        "bash -n /tmp/prepare.sh",
        "sh -n install.sh",
    ],
)
def test_the_remaining_bounded_commands_need_no_wrapper(command):
    assert hook.is_bounded(command) is True
    assert hook.decide(payload("Bash", command)) == (0, "")


def test_verbose_still_revokes_the_new_silent_on_success_entries():
    """`-v` is what turns a fixed confirmation into per-file output."""
    assert hook.is_bounded("git checkout -v main") is False


# --- the guarantee is unchanged for everything the gate exists to catch ---------


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "ls -R /",
        "cat setup.py",
        "git status",
        "git status --short",
        "git diff",
        "find . -name '*.py'",
        "grep -r foo .",
        "cat big | grep x",
        # One capped statement must still not launder an uncapped one.
        "find / -name x; echo done | head -c 10",
        "git ls-files | head -50 && ls -R /",
    ],
)
def test_the_relaxations_do_not_reach_what_the_gate_is_for(command):
    assert hook.decide(payload("Bash", command))[0] == hook.EXIT_BLOCK


def test_block_message_names_every_spelling_that_counts():
    """An agent that does not know `tail -c` passes will keep rewriting it as `head -c`,
    which is the rewrite that drops the summary it wanted.
    """
    _, msg = hook.decide(payload("Bash", "ls -R /"))
    for spelling in ("head -c N", "tail -c N", "head -N", "tail -N"):
        assert spelling in msg
    assert "redirecting stdout" in msg


def test_get_value_dotted_and_missing():
    obj = {"tool_input": {"command": "x"}}
    assert hook.get_value(obj, "tool_input.command") == "x"
    assert hook.get_value(obj, "missing.path", "tool_input.command") == "x"
    assert hook.get_value(obj, "nope") is None
