"""Tests for the scheduled-failure issue reporter.

**Vendored, so nothing here may name a project.** Every value the script reads arrives
in its environment block, which is what lets these tests build the whole world as a
literal: a fake `subprocess.run` and a dict. No repository, no network, no `gh`.

What they are really pinning is the pair of properties that make the tracker usable
rather than noisy -- it opens exactly one issue per failing workflow no matter how many
nights it fails, and a green run closes it. Either one alone produces a dashboard nobody
trusts: without dedup, a week of failure is seven issues; without the close, the issue
list is a record of things that were fixed days ago.
"""

import json
from types import SimpleNamespace

from conftest import load_module

reporter = load_module("scripts/report-workflow-failure.py")


class FakeRun:
    """Stands in for `subprocess.run`, recording argv and replaying canned results.

    Keyed by the `gh` subcommand pair (`("issue", "list")`), which is the only part of
    an invocation these tests care about.
    """

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, argv, capture_output=False, text=False, check=False):
        self.calls.append(list(argv))
        stdout, returncode, stderr = self.results.get(tuple(argv[1:3]), ("", 0, ""))
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def matching(self, *subcommand) -> list[list[str]]:
        width = len(subcommand)
        return [call for call in self.calls if tuple(call[1 : 1 + width]) == subcommand]


def env(**overrides) -> dict[str, str]:
    base = {
        "WORKFLOW": "Nightly",
        "CONCLUSION": "failure",
        "RUN_URL": "https://example.invalid/run/1",
        "RUN_ATTEMPT": "1",
        "HEAD_BRANCH": "main",
        "HEAD_SHA": "abc1234",
        "TRIGGER": "schedule",
        "OWNER": "owner-login",
    }
    base.update(overrides)
    return base


def listing(*issues) -> dict:
    return {("issue", "list"): (json.dumps(list(issues)), 0, "")}


CREATED = ("https://example.invalid/o/r/issues/7\n", 0, "")


# --- what each conclusion means ------------------------------------------------


def test_a_verdict_of_failure_opens_and_a_verdict_of_success_closes():
    for conclusion in ("failure", "timed_out", "startup_failure"):
        assert reporter.decide(conclusion) == "open", conclusion
    assert reporter.decide("success") == "close"


def test_a_run_that_reached_no_verdict_does_neither():
    """The asymmetry is the point, and it is why `cancelled` is not folded into either.

    Treated as success it would close an issue for a workflow still broken; treated as
    failure it would file one about somebody pressing cancel.
    """
    for conclusion in ("cancelled", "skipped", "neutral", "action_required", "stale", ""):
        assert reporter.decide(conclusion) == "ignore", conclusion


def test_an_inconclusive_run_touches_no_api_at_all():
    run = FakeRun()
    assert reporter.main(env(CONCLUSION="cancelled"), run) == 0
    assert run.calls == [], f"a cancelled run should be free, it made {run.calls}"


def test_a_run_with_no_workflow_name_is_an_error_not_a_silent_pass():
    """Every dedupe decision keys off this name; empty would make one issue per run."""
    run = FakeRun()
    assert reporter.main(env(WORKFLOW=""), run) == 1
    assert run.calls == []


# --- the dedupe key ------------------------------------------------------------


def test_the_title_is_a_pure_function_of_the_workflow_name():
    """Anything varying per run -- a number, a date, a SHA -- would defeat the lookup."""
    assert reporter.issue_title("Nightly") == reporter.issue_title("Nightly")
    assert reporter.issue_title("Nightly") != reporter.issue_title("Mutation")
    assert "Nightly" in reporter.issue_title("Nightly")


def test_the_title_carries_no_colon():
    """Not cosmetic: a `:` is a qualifier separator to GitHub's search grammar, and the
    lookup is one search away from needing to quote it."""
    assert ":" not in reporter.issue_title("Nightly")


def test_the_lookup_matches_a_title_exactly():
    title = reporter.issue_title("Nightly")
    issues = [
        {"number": 3, "title": f"Fix the flake behind '{title}'"},
        {"number": 4, "title": title},
    ]
    assert reporter.find_open_issue(issues, title) == 4


def test_the_lookup_declines_a_merely_similar_title():
    """A substring match would let a human's issue that quotes the tracker be commented
    on for a week and then closed by a green run."""
    title = reporter.issue_title("Nightly")
    assert reporter.find_open_issue([{"number": 3, "title": f"{title} again"}], title) is None
    assert reporter.find_open_issue([], title) is None


# --- the failure path ----------------------------------------------------------


def test_the_first_failure_opens_one_issue_carrying_the_run():
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    assert reporter.main(env(), run) == 0

    created = run.matching("issue", "create")
    assert len(created) == 1, run.calls
    argv = created[0]
    assert reporter.issue_title("Nightly") in argv
    body = argv[argv.index("--body") + 1]
    assert "https://example.invalid/run/1" in body, "the issue must link the run it reports"
    assert "abc1234" in body
    assert reporter.LABEL in argv


def test_opening_an_issue_creates_the_label_it_applies():
    """`gh issue create --label` fails outright on a label the repo does not have, which
    is every repo before its first failure -- so the first report would be the one that
    never lands."""
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    reporter.main(env(), run)
    assert run.matching("label", "create"), run.calls


def test_a_repeat_failure_comments_instead_of_opening_a_second_issue():
    """The property that makes this a tracker rather than a mailing list."""
    existing = {"number": 7, "title": reporter.issue_title("Nightly")}
    run = FakeRun(listing(existing))
    assert reporter.main(env(), run) == 0

    assert not run.matching("issue", "create"), "a week of failure must not be seven issues"
    commented = run.matching("issue", "comment")
    assert len(commented) == 1
    assert "7" in commented[0]


def test_the_new_issue_is_assigned_so_it_reaches_a_dashboard():
    """An issue authored by the bot is in nobody's `created` tab. Assignment is the only
    dashboard tab a workflow can put it in, which is the entire reason this file exists
    rather than a run annotation."""
    run = FakeRun({**listing(), ("issue", "create"): CREATED})
    reporter.main(env(), run)
    edits = run.matching("issue", "edit")
    assert len(edits) == 1
    assert "--add-assignee" in edits[0]
    assert "owner-login" in edits[0]


def test_an_unassignable_owner_costs_the_assignment_and_not_the_issue():
    """`repository_owner` is an organisation on an org-owned repo, and an organisation
    cannot be assigned. That must not turn a reported failure into an unreported one."""
    run = FakeRun(
        {
            **listing(),
            ("issue", "create"): CREATED,
            ("issue", "edit"): ("", 1, "could not assign: not a user"),
        }
    )
    assert reporter.main(env(), run) == 0
    assert run.matching("issue", "create"), "the issue must survive a failed assignment"


def test_a_missing_owner_is_reported_rather_than_passed_over():
    assert "unassigned" in reporter.assign("7", "", FakeRun())


# --- the recovery path ---------------------------------------------------------


def test_a_green_run_closes_the_open_issue():
    """Delete this behaviour and the tracker degrades into a list of failures that were
    fixed days ago -- which is the state that makes an aggregate view unusable."""
    existing = {"number": 7, "title": reporter.issue_title("Nightly")}
    run = FakeRun(listing(existing))
    assert reporter.main(env(CONCLUSION="success"), run) == 0

    closed = run.matching("issue", "close")
    assert len(closed) == 1
    assert "7" in closed[0]
    assert "--comment" in closed[0], "closing silently leaves no record of what fixed it"


def test_a_green_run_with_nothing_open_changes_nothing():
    run = FakeRun(listing())
    assert reporter.main(env(CONCLUSION="success"), run) == 0
    assert not run.matching("issue", "close")
    assert not run.matching("issue", "comment")
    assert not run.matching("label", "create"), "a healthy repo must not be mutated at all"


def test_a_green_run_does_not_close_another_workflows_issue():
    other = {"number": 9, "title": reporter.issue_title("Mutation")}
    run = FakeRun(listing(other))
    assert reporter.main(env(CONCLUSION="success"), run) == 0
    assert not run.matching("issue", "close")


# --- failure handling ----------------------------------------------------------


def test_gh_failures_surface_with_their_stderr():
    run = FakeRun({("issue", "list"): ("", 1, "HTTP 403: Resource not accessible")})
    try:
        reporter.open_issues(run)
    except reporter.GhError as error:
        assert "403" in str(error), "the stderr is the only diagnosable part"
    else:
        raise AssertionError("a failed gh call must not read as an empty issue list")


def test_a_failed_lookup_does_not_fall_through_to_creating_a_duplicate():
    """The dangerous direction: an unreadable issue list that reads as "none open" opens
    a fresh issue every night, on a repo already failing."""
    run = FakeRun({("issue", "list"): ("", 1, "HTTP 403")})
    try:
        reporter.main(env(), run)
    except reporter.GhError:
        pass
    assert not run.matching("issue", "create")


def test_the_body_names_the_workflow_and_the_trigger():
    body = reporter.failure_body(env())
    assert "Nightly" in body
    assert "schedule" in body
    assert "main" in body
