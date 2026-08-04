"""Unit tests for the shared task-branch helpers (scripts/task_branch.py)."""

import datetime as dt
import subprocess

from conftest import load_module

tb = load_module("scripts/task_branch.py")


def _cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    """A CompletedProcess stand-in for injecting fake `git` results."""
    return subprocess.CompletedProcess([], returncode, stdout, "")


class TestDetectDefaultBranch:
    def test_reads_branch_from_origin_head(self):
        def git(*args):
            return _cp(0, "refs/remotes/origin/main\n") if args[0] == "symbolic-ref" else _cp(1)

        assert tb.detect_default_branch(git) == "main"

    def test_master_from_origin_head(self):
        def git(*args):
            return _cp(0, "refs/remotes/origin/master\n") if args[0] == "symbolic-ref" else _cp(1)

        assert tb.detect_default_branch(git) == "master"

    def test_probes_master_when_head_unset_and_no_main(self):
        def git(*args):
            if args[0] == "symbolic-ref":
                return _cp(1)  # origin/HEAD not set
            # rev-parse: only origin/master resolves.
            return _cp(0) if args[-1] == "refs/remotes/origin/master" else _cp(1)

        assert tb.detect_default_branch(git) == "master"

    def test_falls_back_when_nothing_resolves(self):
        assert tb.detect_default_branch(lambda *a: _cp(1)) == "main"
        assert tb.detect_default_branch(lambda *a: _cp(1), fallback="trunk") == "trunk"


class TestParsePrompt:
    def test_extracts_prompt_field(self):
        assert tb.parse_prompt('{"prompt": "add SMS retry"}') == "add SMS retry"

    def test_missing_field_is_empty(self):
        assert tb.parse_prompt('{"other": 1}') == ""

    def test_malformed_json_is_empty(self):
        assert tb.parse_prompt("not json") == ""
        assert tb.parse_prompt("") == ""

    def test_non_string_prompt_is_empty(self):
        assert tb.parse_prompt('{"prompt": 42}') == ""


class TestSlugify:
    def test_basic(self):
        assert tb.slugify("Add SMS retry logic") == "add-sms-retry-logic"

    def test_collapses_punctuation_and_trims(self):
        assert tb.slugify("  Fix: the /webhooks/ bug!! ") == "fix-the-webhooks-bug"

    def test_empty_falls_back(self):
        assert tb.slugify("") == "task"
        assert tb.slugify("!!!") == "task"

    def test_truncates_at_word_boundary(self):
        slug = tb.slugify("word " * 30, max_len=20)
        assert len(slug) <= 20
        assert not slug.endswith("-")


class TestTopic:
    def test_drops_filler_and_keeps_content_words(self):
        assert tb.topic("Can you please add a retry to the SMS sender?") == "add-retry-sms-sender"

    def test_keeps_the_action_verb(self):
        assert tb.topic("I think we should rename the ports module") == "rename-ports-module"

    def test_stops_at_the_first_sentence(self):
        prompt = "Fix the webhook timeout. It also fails on empty payloads sometimes."
        assert tb.topic(prompt) == "fix-webhook-timeout"

    def test_reaches_past_a_one_word_lead_in(self):
        assert (
            tb.topic("Question. Why does the lint hook skip templates?")
            == "lint-hook-skip-templates"
        )

    def test_caps_the_word_count(self):
        assert tb.topic("alpha bravo charlie delta echo foxtrot golf hotel") == (
            "alpha-bravo-charlie-delta-echo-foxtrot"
        )
        assert tb.topic("alpha bravo charlie", max_words=2) == "alpha-bravo"

    def test_all_filler_yields_nothing(self):
        assert tb.topic("can you do this for me please") == ""
        assert tb.topic("") == ""


class TestSlugFromPrompt:
    def test_names_the_branch_after_the_topic_not_the_preamble(self):
        prompt = (
            "The coding agent prompt hook that creates the branch - I think it uses "
            "a generic branch name. Is it possible to do better?"
        )
        assert tb.slug_from_prompt(prompt) == "coding-agent-prompt-hook-creates-branch"

    def test_falls_back_to_raw_text_when_topic_is_empty(self):
        # All-filler prompt: better to slugify the words than to name it "task".
        assert tb.slug_from_prompt("can you do this") == "can-you-do-this"

    def test_falls_back_to_task_when_there_is_nothing_at_all(self):
        assert tb.slug_from_prompt("") == "task"

    def test_respects_the_length_cap(self):
        assert len(tb.slug_from_prompt("supercalifragilistic " * 10)) <= tb.SLUG_MAX_LEN


class TestShouldBranch:
    def test_true_on_default(self):
        assert tb.should_branch("master") is True

    def test_false_on_feature_branch(self):
        assert tb.should_branch("claude/foo-0722") is False

    def test_false_on_detached_head(self):
        assert tb.should_branch("") is False

    def test_respects_custom_default(self):
        assert tb.should_branch("main", default_branch="main") is True
        assert tb.should_branch("master", default_branch="main") is False


class TestBranchName:
    def test_includes_prefix_slug_and_date(self):
        assert tb.branch_name("add-sms", set(), today=dt.date(2026, 7, 22)) == "claude/add-sms-0722"

    def test_disambiguates_collision(self):
        existing = {"claude/add-sms-0722"}
        assert (
            tb.branch_name("add-sms", existing, today=dt.date(2026, 7, 22))
            == "claude/add-sms-0722-2"
        )

    def test_disambiguates_multiple_collisions(self):
        existing = {"claude/x-0722", "claude/x-0722-2", "claude/x-0722-3"}
        assert tb.branch_name("x", existing, today=dt.date(2026, 7, 22)) == "claude/x-0722-4"


class TestCheckoutBase:
    def test_clean_tree_bases_on_origin_master(self):
        assert tb.checkout_base(tree_dirty=False) == "origin/master"

    def test_dirty_tree_bases_on_head(self):
        assert tb.checkout_base(tree_dirty=True) is None

    def test_respects_custom_default_branch(self):
        assert tb.checkout_base(tree_dirty=False, default_branch="main") == "origin/main"


class TestCheckoutArgv:
    def test_cuts_the_branch_off_the_base(self):
        assert tb.checkout_argv("claude/x-0729", "origin/main") == [
            "checkout",
            "--no-track",
            "-b",
            "claude/x-0729",
            "origin/main",
        ]

    def test_no_base_branches_from_head(self):
        # Dirty-tree case: no start point, so the new branch carries the edits.
        assert tb.checkout_argv("claude/x-0729", None) == [
            "checkout",
            "--no-track",
            "-b",
            "claude/x-0729",
        ]

    def test_never_tracks_the_base(self):
        # Regression: without --no-track, `checkout -b <name> origin/<default>`
        # branches from a remote-tracking ref, so autoSetupMerge sets the new
        # branch's upstream to origin/<default> -- and the task's first push (or
        # VS Code "Sync Changes") lands on the default branch instead of
        # publishing a branch to open a PR from.
        for base in ("origin/main", "origin/master", None):
            assert "--no-track" in tb.checkout_argv("claude/x-0729", base)

    def test_no_track_precedes_the_branch_name(self):
        # After a bare `-b`, git reads the next token as the branch name: with the
        # flag misplaced the branch would literally be named "--no-track".
        argv = tb.checkout_argv("claude/x-0729", "origin/main")
        assert argv.index("--no-track") < argv.index("-b")


class TestAutoBranchDecision:
    def test_on_master_clean_bases_on_origin(self):
        assert tb.auto_branch_decision("master", "", tree_dirty=False) == (True, "origin/master")

    def test_on_master_dirty_carries_changes(self):
        assert tb.auto_branch_decision("master", "", tree_dirty=True) == (True, None)

    def test_on_shipped_branch_clean_bases_on_origin(self):
        # The worktree case: sitting on the branch /ship just shipped, tree clean.
        decision = tb.auto_branch_decision("claude/x-0722", "claude/x-0722", tree_dirty=False)
        assert decision == (True, "origin/master")

    def test_on_shipped_branch_dirty_does_not_fire(self):
        # Unshipped edits on top of a shipped branch -- never yank them.
        decision = tb.auto_branch_decision("claude/x-0722", "claude/x-0722", tree_dirty=True)
        assert decision == (False, None)

    def test_on_unrelated_feature_branch_does_not_fire(self):
        # Mid-task on a branch that is not the shipped one.
        decision = tb.auto_branch_decision("claude/y-0722", "claude/x-0722", tree_dirty=False)
        assert decision == (False, None)

    def test_no_marker_only_master_fires(self):
        assert tb.auto_branch_decision("claude/x-0722", "", tree_dirty=False) == (False, None)

    def test_detached_head_does_not_fire(self):
        assert tb.auto_branch_decision("", "claude/x-0722", tree_dirty=False) == (False, None)

    def test_custom_default_branch_bases_on_it(self):
        # On a `main`-default repo, cutting from master's origin would be wrong.
        assert tb.auto_branch_decision("main", "", tree_dirty=False, default_branch="main") == (
            True,
            "origin/main",
        )


class TestPlatformManagesBranch:
    def test_true_when_remote_flag_set(self):
        # Claude Code on the web / mobile sets CLAUDE_CODE_REMOTE=true.
        assert tb.platform_manages_branch({"CLAUDE_CODE_REMOTE": "true"}) is True

    def test_false_when_flag_absent(self):
        assert tb.platform_manages_branch({}) is False

    def test_false_when_flag_not_literal_true(self):
        # Only the literal "true" counts -- mirrors session-start.sh's check.
        assert tb.platform_manages_branch({"CLAUDE_CODE_REMOTE": "1"}) is False
        assert tb.platform_manages_branch({"CLAUDE_CODE_REMOTE": "false"}) is False
        assert tb.platform_manages_branch({"CLAUDE_CODE_REMOTE": ""}) is False
