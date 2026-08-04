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


# --- is_capped / get_value units ---


def test_is_capped_true_and_false():
    assert hook.is_capped("foo | head -c 100") is True
    assert hook.is_capped("plain command") is False


def test_get_value_dotted_and_missing():
    obj = {"tool_input": {"command": "x"}}
    assert hook.get_value(obj, "tool_input.command") == "x"
    assert hook.get_value(obj, "missing.path", "tool_input.command") == "x"
    assert hook.get_value(obj, "nope") is None
