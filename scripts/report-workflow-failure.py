#!/usr/bin/env python3
"""Turn a failed scheduled workflow run into a GitHub issue, and close it when green.

**Why an issue at all.** A workflow run is a per-repository artifact. GitHub's
cross-repository dashboards -- `github.com/issues` and `github.com/pulls` -- aggregate
issues and pull requests and nothing else, so a red nightly is visible only to someone
who opens that one repo's Actions tab and looks. Across a workspace of several repos
that is a poll nobody performs, which makes a failing nightly indistinguishable from a
nightly that has not run: both are silent. An issue is the only artifact a scheduled run
can leave that surfaces without being looked for.

**Why it is assigned.** An issue opened by `github-actions[bot]` is authored by the bot,
so it appears in the dashboard's *Created* tab for nobody. Every tab there is keyed to
the viewer -- created, assigned, mentioned -- and assignment is the only one a workflow
can set on someone else's behalf. Opening the issue and leaving it unassigned reproduces
the invisibility this script exists to remove, which is why the assignment step is here
and why its failure is reported rather than swallowed.

**Why it closes itself.** A tracker that only ever opens issues becomes a list of
already-fixed failures within a week, and a stale aggregate view is worse than none: it
is one that has to be re-verified item by item before it can be believed. The success
path is not an optimisation, it is the half that keeps the failure path meaningful.

Deduplication is by exact issue title, one open issue per workflow name. The label is
for filtering only -- never for lookup, because `gh issue list --label` fails outright on
a repo that has never carried the label, which is every repo before its first failure.

Vendored from devkit (`sync-devkit.py`'s MANIFEST) and byte-identical everywhere:
nothing in it names a project. The repository, the workflow, the run and the owner all
arrive as environment variables from the `workflow_run` event.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

LABEL = "ci-failure"
LABEL_COLOR = "b60205"
LABEL_DESCRIPTION = "A scheduled workflow is failing; opened and closed automatically"

# Conclusions that are evidence the run reached a verdict. Everything else --
# `cancelled`, `skipped`, `neutral`, `action_required`, `stale` -- is deliberately
# neither: a cancelled run is not evidence of health, so closing on it would hide a
# real failure, and it is not evidence of breakage either, so opening on it would file
# an issue about somebody pressing the cancel button.
FAILING = ("failure", "timed_out", "startup_failure")
PASSING = ("success",)

# How many open issues to scan for the tracking one. Dedup is by title over this
# window, so a repo with more open issues than this opens a second tracker rather than
# commenting on the first -- a duplicate, which is the harmless direction. The other
# direction, matching the wrong issue and then closing it, is impossible: the match is
# exact and the title names the workflow.
ISSUE_SCAN_LIMIT = 200


class GhError(RuntimeError):
    """A `gh` invocation that failed, with its stderr attached."""


def decide(conclusion: str) -> str:
    """`open`, `close`, or `ignore` for a run conclusion."""
    if conclusion in PASSING:
        return "close"
    if conclusion in FAILING:
        return "open"
    return "ignore"


def issue_title(workflow: str) -> str:
    """The dedupe key, and the reason it carries no `:`.

    It is matched exactly, so it must be a pure function of the workflow name -- no run
    number, no date, nothing that differs between two failures of the same workflow.
    """
    return f"{workflow} workflow is failing"


def run_facts(env: dict[str, str]) -> list[tuple[str, str]]:
    """The run's identifying details, in the order they are worth reading."""
    return [
        ("Run", env.get("RUN_URL", "(unknown)")),
        ("Attempt", env.get("RUN_ATTEMPT", "(unknown)")),
        ("Conclusion", env.get("CONCLUSION", "(unknown)")),
        ("Branch", env.get("HEAD_BRANCH", "(unknown)")),
        ("Commit", env.get("HEAD_SHA", "(unknown)")),
        ("Triggered by", env.get("TRIGGER", "(unknown)")),
    ]


def facts_table(env: dict[str, str]) -> str:
    rows = "\n".join(f"| {key} | {value} |" for key, value in run_facts(env))
    return f"| | |\n| --- | --- |\n{rows}"


def failure_body(env: dict[str, str]) -> str:
    workflow = env.get("WORKFLOW", "the workflow")
    return (
        f"`{workflow}` failed, and no change was pushed to trigger it -- so nothing "
        "else was going to report this.\n\n"
        f"{facts_table(env)}\n\n"
        "The usual causes are the ones a PR gate is structurally blind to: a dependency "
        "published inside this project's version bounds, a runner image bump, an "
        "expired credential, or a test that is flaky rather than broken.\n\n"
        "---\n\n"
        "This issue is opened, updated and closed by "
        "`.github/workflows/scheduled-failure-issue.yml`. It closes itself on the next "
        "successful run of the same workflow. Closing it by hand is fine -- the next "
        "failure opens a fresh one."
    )


def recurrence_comment(env: dict[str, str]) -> str:
    return f"Still failing.\n\n{facts_table(env)}"


def recovery_comment(env: dict[str, str]) -> str:
    workflow = env.get("WORKFLOW", "The workflow")
    return f"`{workflow}` succeeded, so this is closing itself.\n\n{facts_table(env)}"


def find_open_issue(issues: list[dict], title: str) -> int | None:
    """The number of the open issue tracking `title`, or None.

    Exact match: a substring match would let an unrelated issue that quotes the title in
    passing be commented on, and then closed by a green run.
    """
    for issue in issues:
        if issue.get("title") == title:
            number = issue.get("number")
            return None if number is None else int(number)
    return None


def gh(args: list[str], run) -> str:
    """Run `gh` and return stdout, raising with stderr attached on a non-zero exit.

    `check=False` plus an explicit raise rather than `check=True`: a
    `CalledProcessError` prints the argv and drops the stderr, and every failure mode
    here (a missing label, an unassignable owner, a token without `issues: write`) is
    diagnosable only from what `gh` wrote to stderr.
    """
    result = run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def open_issues(run) -> list[dict]:
    raw = gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            str(ISSUE_SCAN_LIMIT),
            "--json",
            "number,title",
        ],
        run,
    )
    return json.loads(raw or "[]")


def ensure_label(run) -> None:
    """Create the label if it is absent.

    `--force` is what makes this both idempotent and safe on a repo that has never had
    the label -- the same reason `dependabot-automerge.yml` uses it before its first
    `gh pr edit --add-label`, where a missing label failed the whole job on something
    unrelated to the bump it was judging.
    """
    _ = gh(
        [
            "label",
            "create",
            LABEL,
            "--color",
            LABEL_COLOR,
            "--force",
            "--description",
            LABEL_DESCRIPTION,
        ],
        run,
    )


def assign(issue: str, owner: str, run) -> str:
    """Assign `issue` to `owner`, returning a line describing what happened.

    Tolerant by design, and a separate call from creation for that reason.
    `repository_owner` is an organisation for an organisation-owned repo, and an
    organisation cannot be assigned; a user without write access cannot be either. Both
    are configuration facts this workflow has no way to know and no business failing the
    run over -- but they cost the issue its place in the owner's dashboard, so they are
    reported rather than passed over.
    """
    if not owner:
        return "no owner given, so it is unassigned and will not reach a dashboard"
    try:
        gh(["issue", "edit", issue, "--add-assignee", owner], run)
    except GhError as error:
        return (
            f"could not assign to {owner!r} ({error}). The issue exists but appears in "
            "nobody's assigned view -- assign it by hand, or point OWNER at an "
            "assignable user."
        )
    return f"assigned to {owner}"


def main(env: dict[str, str] | None = None, run=None) -> int:
    env = dict(os.environ) if env is None else env
    run = subprocess.run if run is None else run

    workflow = env.get("WORKFLOW", "").strip()
    if not workflow:
        print("WORKFLOW is empty -- nothing to track.", file=sys.stderr)
        return 1

    conclusion = env.get("CONCLUSION", "").strip()
    action = decide(conclusion)
    if action == "ignore":
        print(f"{workflow} concluded {conclusion!r}: no verdict either way, doing nothing.")
        return 0

    title = issue_title(workflow)
    existing = find_open_issue(open_issues(run), title)

    if action == "close":
        if existing is None:
            print(f"{workflow} passed and nothing was open. Nothing to do.")
            return 0
        gh(
            [
                "issue",
                "close",
                str(existing),
                "--reason",
                "completed",
                "--comment",
                recovery_comment(env),
            ],
            run,
        )
        print(f"{workflow} passed; closed #{existing}.")
        return 0

    if existing is not None:
        gh(["issue", "comment", str(existing), "--body", recurrence_comment(env)], run)
        print(f"{workflow} failed again; commented on #{existing}.")
        return 0

    ensure_label(run)
    url = gh(
        ["issue", "create", "--title", title, "--body", failure_body(env), "--label", LABEL],
        run,
    ).strip()
    print(f"{workflow} failed; opened {url} ({assign(url, env.get('OWNER', ''), run)}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
