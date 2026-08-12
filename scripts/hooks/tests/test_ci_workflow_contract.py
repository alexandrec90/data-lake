"""Contract: every project carries the same baseline CI surface.

**This file is vendored into every consuming project**, so nothing here may assert a
value specific to one project. What it asserts is devkit's policy about *which* GitHub
Actions files every repo has, plus the cross-cutting settings that make a workflow safe
to leave running unattended.

Why a contract test rather than more vendoring. One of the required files carries no
per-project content and is therefore shipped whole: `dependabot-automerge.yml` is in
`sync-devkit.py`'s MANIFEST and drift-checked byte-for-byte. The rest cannot be. A
`dependabot.yml` names the ecosystems this project actually has; a gate or a nightly
names its services, its migrations and its frontend tier, and carameli's five-job gate
is the standing proof that a shared one would have to delete real work or live
permanently exempted.

`setup-python-env/action.yml` was briefly in that first group and is not any more: how
a project installs its dependencies is exactly the kind of thing that varies. Vendoring
it deleted apt-finder's private-sibling clone and handed carameli a `uv sync` it has no
lock for. Rendered from `templates/` now, like the gate.

So those two are rendered from `templates/`, which is a **one-shot copy**: `--pull`
never looks at a template again. Nothing checked that a project still had one, or had
ever been handed one. It showed. Across the six repos in this workspace: one had a
nightly, five had a dependency-update config, and two had no way to merge a Dependabot
PR at all. Every one of those gaps is silent -- a repo with no nightly does not fail,
it simply never learns that the world moved underneath a default branch nobody pushed
to, and a repo with no auto-merge accumulates bot PRs that pass their gate and sit.

This file is the half `--pull` cannot do: it does not supply the workflow, it refuses
to let a project go without one.

Stdlib-only text parsing, no PyYAML -- the vendored suite runs as its own step and may
not assume the project's environment holds anything but the standard library.
"""

import re
from pathlib import Path

from conftest import REPO_ROOT

WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

PR_GATE = "pr-gate.yml"
AUTOMERGE = "dependabot-automerge.yml"
NIGHTLY = "nightly.yml"

# The title `dependabot-automerge.yml` waits on. That file is vendored byte-identical,
# so it cannot parameterise the name -- every project's gate is titled `PR Gate`.
GATE_TITLE = "PR Gate"

# What a project is told to write when it has no nightly. It lives in the failing
# assertion because the remedy for a missing *template* render is the one thing
# `--pull` can never deliver.
NIGHTLY_REMEDY = """\
Add .github/workflows/nightly.yml -- the scheduled full run against the default
branch. The PR gate only fires on a change, so it is structurally blind to every
failure that arrives without one: a dependency published inside this project's
version bounds, a runner image bump, an expired credential, a test that is flaky
rather than broken. At minimum:

    name: Nightly
    on:
      schedule:
        - cron: "0 2 * * *"
      workflow_dispatch:
    permissions:
      contents: read
    concurrency:
      group: ${{ github.workflow }}
      cancel-in-progress: false
    jobs:
      full-suite:
        runs-on: ubuntu-latest
        steps:
          - uses: actions/checkout@v7
          - uses: ./.github/actions/setup-python-env
            with:
              python-version: "3.12"
          - run: uv run python scripts/run-tests.py
          - run: uv run python -m pytest scripts/hooks/tests/ -q

Add this project's own tiers (services, migrations, a frontend suite) the way its
PR gate does. devkit renders one for new projects; an older project predates that
and has to be given one by hand, because templates/ is a one-shot copy."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Lines whose first non-space character is not `#`, blanks dropped.

    Every scan below runs over these. Workflow headers in this harness deliberately
    *name* the trigger they must never grow ("it must NEVER gain a `schedule:`"), so a
    scan that read comments would find the word it was looking for inside the very
    paragraph forbidding it.
    """
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _workflows() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml"))


def _top_level_block(text: str, key: str) -> list[str] | None:
    """The lines under a column-0 `key:`, indentation preserved, or None if absent.

    An empty list means the key is present with no indented body (`permissions: {}`),
    which is a real shape and distinct from the key not being there at all -- so
    callers test against None, never against falsiness.
    """
    lines = _code_lines(text)
    for index, line in enumerate(lines):
        if not re.fullmatch(rf"{re.escape(key)}:\s*(?:\S.*)?", line):
            continue
        body = []
        for following in lines[index + 1 :]:
            if not following.startswith((" ", "\t")):
                break
            body.append(following)
        return body
    return None


def _direct_keys(body: list[str]) -> list[str]:
    """The mapping keys directly under a block, ignoring anything nested deeper.

    `on:` carries `push:` and, under it, `branches:`. Both look like keys to a flat
    regex, and "the workflow has a `branches` trigger" is the sort of nonsense that
    makes a scan pass for the wrong reason.
    """
    indents = [len(ln) - len(ln.lstrip()) for ln in body]
    if not indents:
        return []
    depth = min(indents)
    return [
        match.group(1)
        for ln in body
        if len(ln) - len(ln.lstrip()) == depth and (match := re.match(r"\s*([A-Za-z_][\w-]*):", ln))
    ]


def _triggers(text: str) -> list[str]:
    return _direct_keys(_top_level_block(text, "on") or [])


# --- the files every project has ----------------------------------------------


def test_the_workflow_scan_is_not_vacuous():
    """Guard against every loop below passing over an empty set.

    Not a skip: a repo running this suite is a repo whose PR gate invoked it, so an
    absent workflow directory is not "no CI tier here", it is the tier having moved
    somewhere nothing checks.
    """
    assert WORKFLOWS_DIR.is_dir(), f"{WORKFLOWS_DIR.relative_to(REPO_ROOT)} does not exist"
    assert _workflows(), "no *.yml under .github/workflows -- the layout changed"


def test_dependency_updates_are_configured():
    """`.github/dependabot.yml`, with the actions ecosystem in it.

    The actions entry is asserted specifically because it is the one every project has
    and the one most often left out: a workflow pins `actions/checkout@v7`, and with no
    `github-actions` ecosystem declared nothing on earth moves that pin. The Python,
    npm and docker entries are the project's own call -- they follow from which
    manifests it actually ships.
    """
    assert DEPENDABOT.is_file(), (
        ".github/dependabot.yml is missing. Without it nothing opens an update PR, so "
        "the auto-merge workflow this project already carries has nothing to act on, "
        "and every pin ages in place -- security advisories included."
    )
    text = _read(DEPENDABOT)
    assert re.search(r"^version:\s*2\s*$", text, re.M), (
        ".github/dependabot.yml declares no `version: 2`; GitHub ignores the file"
    )
    ecosystems = set(re.findall(r"package-ecosystem:\s*['\"]?([\w-]+)", text))
    assert "github-actions" in ecosystems, (
        f".github/dependabot.yml covers {sorted(ecosystems)} but not `github-actions`. "
        "Every workflow here pins actions by major, and nothing else moves those pins."
    )


def test_the_pr_gate_exists_and_carries_the_title_automerge_waits_on():
    """`workflow_run` matches by title, so a rename makes the merge job inert.

    Nothing goes red when it does: an unmatched `workflow_run` produces no run at all,
    so bot PRs simply sit open behind a gate that passed.
    """
    gate = WORKFLOWS_DIR / PR_GATE
    assert gate.is_file(), f".github/workflows/{PR_GATE} is missing"
    assert re.search(rf"^name:\s*{re.escape(GATE_TITLE)}\s*$", _read(gate), re.M), (
        f"{PR_GATE} is not titled {GATE_TITLE!r}. {AUTOMERGE} is vendored "
        "byte-identical and waits on that exact title in every project."
    )


def test_dependabot_prs_have_something_that_merges_them():
    assert (WORKFLOWS_DIR / AUTOMERGE).is_file(), (
        f".github/workflows/{AUTOMERGE} is missing -- run "
        "`python scripts/sync-devkit.py --pull`. It is a MANIFEST file, so it arrives "
        "whole; a project without it collects passing bot PRs nobody merges, which is "
        "the state that makes people turn Dependabot off."
    )


def test_a_scheduled_full_run_exists():
    nightly = WORKFLOWS_DIR / NIGHTLY
    assert nightly.is_file(), NIGHTLY_REMEDY
    triggers = _triggers(_read(nightly))
    assert "schedule" in triggers, (
        f"{NIGHTLY} declares {sorted(triggers)} and no `schedule:` -- it is the "
        "scheduled tier or it is nothing."
    )
    assert "workflow_dispatch" in triggers, (
        f"{NIGHTLY} has no `workflow_dispatch:`. A scheduled run that cannot also be "
        "triggered by hand can only be debugged one attempt per day."
    )


# --- the settings that make an unattended run safe -----------------------------


def test_every_workflow_declares_top_level_permissions():
    for path in _workflows():
        assert _top_level_block(_read(path), "permissions") is not None, (
            f"{path.name}: no top-level `permissions:` block, so it inherits whatever "
            "the repository default is -- write, on a repo nobody narrowed. Declare "
            "`permissions:` / `  contents: read`; a job needing more opts in with its "
            "own block."
        )


def test_every_workflow_declares_concurrency_except_the_automerge_one():
    for path in _workflows():
        if path.name == AUTOMERGE:
            # Deliberately omitted there: its merge job is driven by workflow_run
            # completion events, and a concurrency group would drop one as superseded.
            continue
        assert _top_level_block(_read(path), "concurrency") is not None, (
            f"{path.name}: no top-level `concurrency:` block, so redundant runs pile "
            "up on the same ref."
        )


def test_a_scheduled_run_is_allowed_to_finish():
    checked = 0
    for path in _workflows():
        text = _read(path)
        if "schedule" not in _triggers(text):
            continue
        checked += 1
        body = _top_level_block(text, "concurrency") or []
        cancels = [ln.strip() for ln in body if ln.strip().startswith("cancel-in-progress:")]
        assert cancels, (
            f"{path.name} is scheduled but its concurrency group leaves "
            "`cancel-in-progress` unset, which defaults to false only by convention. "
            "State it."
        )
        value = cancels[0].split(":", 1)[1].strip()
        assert value == "false", (
            f"{path.name}: a scheduled run must be allowed to finish "
            f"(`cancel-in-progress: false`), not {value!r}. A manual dispatch during "
            "the scheduled window, or a slow run overrunning into the next one, would "
            "otherwise kill the only unattended signal the default branch gets."
        )
    assert checked, "no scheduled workflow was examined -- the trigger scan is inert"


USES = re.compile(r"uses:\s*([\w.-]+/[\w./-]+)@(\S+)")
IMMUTABLE_REF = re.compile(r"v?\d+(?:\.\d+)*|[0-9a-f]{40}")


def test_third_party_actions_are_pinned_to_an_immutable_ref():
    """A branch pin lets someone else's push change what this repo runs.

    Local composite actions (`uses: ./.github/actions/...`) carry no `@ref` and so are
    not matched -- they are this repo's own files, gated like anything else in it.
    """
    floating = [
        f"{path.name}: {action}@{ref}"
        for path in _workflows()
        for action, ref in USES.findall(_read(path))
        if not IMMUTABLE_REF.fullmatch(ref)
    ]
    assert not floating, f"actions pinned to a mutable ref: {floating}"


# --- the parsers above, on inputs whose answer is known ------------------------


def test_block_parser_reads_only_column_zero_keys():
    text = "on:\n  push:\n    branches: [main]\njobs:\n  build:\n    concurrency: x\n"
    assert _top_level_block(text, "on") == ["  push:", "    branches: [main]"]
    # That `concurrency:` is a job's, not the workflow's -- the distinction the
    # permissions and concurrency checks rest on.
    assert _top_level_block(text, "concurrency") is None


def test_block_parser_distinguishes_an_empty_body_from_an_absent_key():
    assert _top_level_block("permissions: {}\njobs:\n", "permissions") == []
    assert _top_level_block("jobs:\n", "permissions") is None


def test_block_parser_ignores_commented_out_keys():
    commented = "# schedule:\n#   - cron: '0 2 * * *'\non:\n  push:\n"
    assert _top_level_block(commented, "schedule") is None


def test_direct_keys_ignores_nested_mappings():
    body = ["  push:", "    branches: [main]", "  schedule:", "    - cron: '0 2 * * *'"]
    assert _direct_keys(body) == ["push", "schedule"]


def test_immutable_ref_accepts_tags_and_shas_and_rejects_branches():
    for good in ("v7", "v1.2.3", "3.12.1", "a" * 40):
        assert IMMUTABLE_REF.fullmatch(good), good
    for bad in ("main", "master", "v7-beta", "latest"):
        assert not IMMUTABLE_REF.fullmatch(bad), bad
