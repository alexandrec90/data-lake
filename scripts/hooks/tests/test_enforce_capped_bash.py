"""Unit tests for the enforce-capped-bash PreToolUse hook decision logic.

Vendored tier: `decide` takes an injectable `max_bytes` precisely so these tests do
not depend on the `[bash]` block of whichever repo they run in.
"""

import json

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


def test_a_bounded_prefix_does_not_exempt_a_longer_word():
    """`date` is bounded; `dates_report.sh` is a different command entirely."""
    assert hook.is_bounded("dateutil-dump --all") is False
    assert hook.is_bounded("echoes-everything") is False


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


def test_get_value_dotted_and_missing():
    obj = {"tool_input": {"command": "x"}}
    assert hook.get_value(obj, "tool_input.command") == "x"
    assert hook.get_value(obj, "missing.path", "tool_input.command") == "x"
    assert hook.get_value(obj, "nope") is None
