"""Contract tests: the repo satisfies what the vendored harness already assumes.

**This file is vendored into every consuming project**, so nothing here may assert
a value specific to one project. Every check derives from that repo's own
`.devkit.toml` (via `harness_config`) and from `stop.py`'s own reachability
logic, and any check that cannot be decided from config is left out rather than
guessed at.

The gap this closes. `stop.py` is vendored byte-identical everywhere and dispatches
to project-owned sibling scripts, and a missing one fails in
whichever direction is least visible:

  - Where `_command_for` declines to build a command, the tier is skipped. Correct --
    a local tooling gap must never block the agent -- and also invisible: a project
    whose `lint-all.py` was never rendered has a Stop gate that reports green having
    run nothing. `stop.py`'s `_REQ_RE` not matching `uv.lock` was this exact shape,
    "silently inert in every uv-native project -- it never fired, so nothing looked
    broken."
  - Where it *does* build one, a missing script is worse than a skip. The interpreter
    exists, so the spawn succeeds and Python exits 2 with "can't open file" --
    indistinguishable from a real finding, and unfixable from the source tree. Every
    generated project blocked its own Stop on a bogus `lock-markers` failure the
    moment a lockfile changed, until `_command_for` learned to check.

Neither is something the runtime should escalate on, so CI is where it gets noticed.
These tests are that second half.

`check-lock-markers.py` is deliberately not asserted: the tier is project-owned
(its sentinels name that project's own lockfiles), so "absent" means "no such tier",
not "broken". It is an explicit skip in `stop.py`, not an accidental one.
"""

import ast
import dataclasses
import inspect
import json
from collections.abc import Mapping
import re
from pathlib import Path

import pytest
from conftest import REPO_ROOT, load_module

cfg = load_module("scripts/hooks/harness_config.py")
hook = load_module("scripts/hooks/stop.py")

CFG = hook.CFG
SETTINGS = REPO_ROOT / ".claude" / "settings.json"
CODEX_HOOKS = REPO_ROOT / ".codex" / "hooks.json"
CODEX_ROOT_PATH_RE = re.compile(
    r'(?:\$\(git rev-parse --show-toplevel\)|__CODEX_PROJECT_ROOT__)/([^"\r\n]+)'
)
CODEX_LAUNCHER_ADAPTER_MARKER = "r/'scripts/hooks/codex-hook-adapter.py'"


def _wires_stop_hook() -> bool:
    """True when this repo actually registers `stop.py` as a Stop hook.

    The gate for every check below that reads the repo's shape off its manifest. A repo
    that vendors these tests without wiring the hook has not adopted the tier they
    describe, so asserting its files against a manifest nothing acts on would report a
    failure about a tier nobody is running. Wiring the hook is the repo making a real
    claim about itself.

    This docstring used to justify the gate differently -- devkit's own `.devkit.toml`
    being a *fixture* that "turns on the DB and frontend tiers" and describes a project
    shaped nothing like devkit. That stopped being true when the manifest was rewritten
    to describe devkit: both tiers are off, devkit does wire the hook, and the checks
    below run here like anywhere else. The sentence outlived the fact by months, in a
    file every project vendors — which is why a claim about a repo's shape belongs in
    the assertion, where it fails, and not only in the prose above it.
    """
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    entries = settings.get("hooks", {}).get("Stop", [])
    if not isinstance(entries, list):
        return False
    return any(
        "stop.py" in h.get("command", "")
        for entry in entries
        if isinstance(entry, dict)
        for h in entry.get("hooks", [])
        if isinstance(h, dict)
    )


consumes_harness = pytest.mark.skipif(
    not _wires_stop_hook(),
    reason="repo does not wire stop.py as a Stop hook (harness source repo, not a consumer)",
)


# --- the committed manifest is spelled correctly ------------------------------
# Ungated: a typo in a fixture manifest is still a typo.


def _toml_schema() -> dict[str, frozenset[str]]:
    """Legal keys per `.devkit.toml` table, mirroring `from_dict`.

    The three hand-mapped tables are spelled out because `from_dict` renames their
    keys (`[paths] app` -> `app_dir`); the rest map 1:1 onto their dataclass, so
    they are derived and cannot drift as fields are added.
    """
    fields = lambda dc: frozenset(f.name for f in dataclasses.fields(dc))
    return {
        "project": frozenset({"env_prefix"}),
        "paths": frozenset({"app", "tests", "unit_tests"}),
        "db": fields(cfg.DbConfig),
        "frontend": fields(cfg.FrontendConfig),
        "python": fields(cfg.PythonConfig),
        "bash": fields(cfg.BashConfig),
        "docker": fields(cfg.DockerConfig),
    }


def test_manifest_has_no_unknown_keys():
    """An unrecognised key silently disables the tier it was meant to configure.

    `from_dict` is all `raw.get(name, default)` and never inspects what it did not
    consume, by design -- a config typo must not break the Stop hook. The cost is
    that `db_servce = "db"` reads as "no db_service was set", the DB tier quietly
    falls back to a default that does not match the compose file, and the tier stops
    doing anything. Nothing raises, nothing logs, and CI stays green. This is the
    only place that difference is ever visible.
    """
    tomllib = pytest.importorskip("tomllib")
    manifest = REPO_ROOT / cfg.MANIFEST_NAME
    if not manifest.exists():
        pytest.skip(f"no {cfg.MANIFEST_NAME} (harness runs on neutral defaults)")

    with manifest.open("rb") as fh:
        raw = tomllib.load(fh)

    schema = _toml_schema()
    unknown = [k for k in raw if k not in schema]
    assert not unknown, f"unknown table(s) in {cfg.MANIFEST_NAME}: {sorted(unknown)}"

    for table, allowed in schema.items():
        section = raw.get(table)
        if not isinstance(section, dict):
            continue
        # `[db.test_env]` is an open map of env-var names -> defaults, so its keys
        # are the project's to choose; only the table itself must be spelled right.
        extra = sorted(set(section) - allowed)
        assert not extra, f"unknown key(s) in [{table}]: {extra} (legal: {sorted(allowed)})"


# --- the scripts stop.py dispatches to are actually there ---------------------


@consumes_harness
def test_unconditional_lint_tier_has_its_script():
    """`lint-all.py` backs the one tier that runs on every non-empty diff.

    `select_checks` adds CHECK_LINT whenever anything changed at all -- there is no
    config field that turns it off. If the script is missing, the Stop gate's only
    always-on check is a no-op in every session.
    """
    assert hook.LINT_ALL.exists(), f"{hook.LINT_ALL.relative_to(REPO_ROOT)} is missing"
    assert hook._command_for(hook.CHECK_LINT) is not None


def _declared_interface(path: Path) -> str | None:
    """A script's usage text: its module-level `USAGE` string, else its docstring.

    Parsed with `ast` rather than imported -- the runner pulls in dependencies this
    vendored test cannot assume, and parsing costs nothing. Returns None when the
    script declares neither, which means there is no interface to check against.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        if any(isinstance(t, ast.Name) and t.id == "USAGE" for t in node.targets):
            return node.value.value
    return ast.get_docstring(tree)


@consumes_harness
def test_the_remediation_command_exists():
    """The failure message tells the agent to run `run-tests.py`; it must be there.

    `_print_verify_failures` signs off with "Re-run locally: ... | python
    scripts/run-tests.py --changed". A gate whose advice on failure is a path that
    does not exist sends the agent in a circle at precisely the worst moment.
    """
    assert (REPO_ROOT / "scripts" / "run-tests.py").exists()


@consumes_harness
def test_the_remediation_flags_are_accepted_by_the_scripts_they_name():
    """Existing is not enough -- the runner must accept the FLAG the message passes.

    A runner that rejects unknown arguments (the strict-args rule) exits 2 on advice
    it was handed, which is worse than no advice: the agent sees a failure from the
    remediation step itself. carameli shipped exactly that -- its runner spelled the
    changed-only flag `--fast` while this hook has always said `--changed`, so the
    Stop gate's sign-off died on "Unknown argument: --changed".

    Checked against the runner's *declared interface* -- its `USAGE` literal, or its
    module docstring when it has no USAGE -- rather than by importing or running it:
    a project's runner pulls in its own dependencies (this test is vendored into repos
    that have none of them) and executing it would run the suite.

    Scoping to the declared interface rather than the whole file is what gives the
    check teeth. A free-text search over the source also matches the old flag's own
    deprecation note, so it passes on precisely the repo that is broken.
    """
    remediation = inspect.getsource(hook._print_verify_failures)
    checked = 0
    for script, flag in re.findall(r"python (scripts/[\w-]+\.py) (--[\w-]+)", remediation):
        path = REPO_ROOT / script
        if not path.exists():
            continue  # covered by the test above; not this one's job to duplicate
        declared = _declared_interface(path)
        if declared is None:
            continue
        checked += 1
        assert flag in declared, (
            f"stop.py tells the agent to run `{script} {flag}`, but that flag is not in "
            f"{script}'s usage text -- a strict-args runner will exit 2 on its own advice"
        )
    assert checked or not _wires_stop_hook(), (
        "no remediation flag could be checked: either stop.py stopped naming one, or "
        "the runners it names declare no usage text at all"
    )


@consumes_harness
def test_optional_tiers_skip_explicitly_when_absent():
    """A tier whose project-owned script is absent must resolve to None, not argv.

    Guards the `_command_for` early-returns: without them a missing script reaches
    `subprocess.run` and is skipped by the OSError handler instead, which cannot be
    told apart from the script existing and failing to start.
    """
    for check in (hook.CHECK_LINT, hook.CHECK_LOCKS):
        spec = hook._command_for(check)
        script = Path(spec[0][1]) if spec else None
        assert spec is None or script.exists(), f"{check} resolved to a missing {script}"


# --- the manifest's paths describe files that exist ---------------------------


@consumes_harness
def test_configured_paths_exist():
    """`[paths]` drives which checks a diff selects; a stale entry silences them.

    `host_test_targets` and `select_checks` decide entirely by string prefix
    (`path.startswith(CFG.app_dir)`), so renaming `app/` to `src/` without updating
    the manifest does not error -- no changed path matches any more, the DB tier
    resolves to an empty target list, and verification passes by running nothing.
    """
    for label, value in (
        ("[paths] app", CFG.app_dir),
        ("[paths] tests", CFG.tests_dir),
        ("[paths] unit_tests", CFG.unit_tests),
    ):
        assert (REPO_ROOT / value).is_dir(), f"{label} = {value!r} is not a directory"


@consumes_harness
def test_frontend_paths_exist_when_the_tier_is_on():
    """Same prefix-matching trap, one tier over: `_is_frontend` is a `startswith`."""
    if not CFG.frontend.enabled:
        pytest.skip("project has no frontend tier")
    assert (REPO_ROOT / CFG.frontend.dir).is_dir(), f"[frontend] dir = {CFG.frontend.dir!r}"
    assert (REPO_ROOT / CFG.frontend.src).is_dir(), f"[frontend] src = {CFG.frontend.src!r}"


def undefined_npm_scripts(commands: dict[str, list[str]], scripts: Mapping[str, str]) -> list[str]:
    """Labels whose command is `npm run <name>` for a name `scripts` does not define.

    Only the `run` form is checked: a command that is not `npm run` names a binary, and
    whether that resolves is npm's business rather than the manifest's.
    """
    return [
        label
        for label, command in commands.items()
        if len(command) == 2 and command[0] == "run" and command[1] not in scripts
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["run", "lint:types"], []),
        (["run", "typecheck"], ["[frontend] typecheck_cmd"]),
        # Not `npm run`, so not this check's business either way.
        (["exec", "tsc", "--noEmit"], []),
        (["run"], []),
    ],
)
def test_undefined_npm_scripts_flags_only_the_run_form(command, expected):
    scripts = {"lint:types": "tsc --noEmit", "test:run": "vitest run"}
    assert undefined_npm_scripts({"[frontend] typecheck_cmd": command}, scripts) == expected


@consumes_harness
def test_frontend_commands_name_scripts_that_exist():
    """`npm run <name>` for a name `package.json` does not define is a failing tier.

    Both frontend commands are `["run", <script>]` handed to npm, and npm answers an
    undefined script with `Missing script` and a non-zero exit. So a renamed script does
    not disable the tier quietly the way a stale `[paths]` prefix does -- it fails every
    diff that selects it, with a message about npm rather than about the manifest, and
    the manifest is where the fix is. Carameli's `typecheck_cmd` named `typecheck` for
    long enough to be copied into its `CLAUDE.md` as a caveat; the script is `lint:types`.
    """
    if not CFG.frontend.enabled:
        pytest.skip("project has no frontend tier")
    manifest = REPO_ROOT / CFG.frontend.dir / "package.json"
    if not manifest.exists():
        pytest.skip(f"no {manifest.relative_to(REPO_ROOT)}")

    scripts = json.loads(manifest.read_text(encoding="utf-8")).get("scripts", {})
    missing = undefined_npm_scripts(
        {
            "[frontend] test_cmd": list(CFG.frontend.test_cmd),
            "[frontend] typecheck_cmd": list(CFG.frontend.typecheck_cmd),
        },
        scripts,
    )
    assert not missing, (
        f"{', '.join(missing)} names an npm script package.json does not define "
        f"(defined: {sorted(scripts)})"
    )


# --- generated Codex handlers -------------------------------------------------


def _codex_commands(payload: dict) -> list[tuple[str, str]]:
    """Return ``(event, command)`` pairs without assuming a project's topology."""
    commands: list[tuple[str, str]] = []
    hooks = payload.get("hooks", {})
    assert isinstance(hooks, dict), ".codex/hooks.json: `hooks` must be an object"
    for event, groups in hooks.items():
        assert isinstance(groups, list), f".codex/hooks.json: {event} must be a list"
        for group in groups:
            assert isinstance(group, dict), f".codex/hooks.json: {event} group must be an object"
            handlers = group.get("hooks", [])
            assert isinstance(handlers, list), f".codex/hooks.json: {event}.hooks must be a list"
            for handler in handlers:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command = handler.get("command")
                assert isinstance(command, str), (
                    f".codex/hooks.json: {event} command hook has no string command"
                )
                commands.append((event, command))
    return commands


def _codex_command_paths(command: str) -> list[str]:
    """Repo-relative files named by either generated launcher generation."""
    paths = CODEX_ROOT_PATH_RE.findall(command)
    if CODEX_LAUNCHER_ADAPTER_MARKER in command:
        paths.append("scripts/hooks/codex-hook-adapter.py")
    return paths


def test_codex_command_paths_cover_the_launcher_and_handler():
    command = (
        "python3 -c \"r/'scripts/hooks/codex-hook-adapter.py'\" --event Stop -- "
        'python3 "__CODEX_PROJECT_ROOT__/scripts/hooks/stop.py"'
    )
    assert _codex_command_paths(command) == [
        "scripts/hooks/stop.py",
        "scripts/hooks/codex-hook-adapter.py",
    ]


def test_generated_codex_handlers_exist():
    """Every git-root path emitted into an opted-in repo must name a real file.

    Converter unit tests prove the JSON rewrite. They cannot prove a consumer
    received the adapter, session bridge, or project-owned handler named by that
    JSON. This is the runtime half of the contract: an absent handler is a hook that
    looks configured, is trusted successfully, and fails only when its event fires.
    """
    if not CODEX_HOOKS.is_file():
        pytest.skip("repo has not opted into project-local Codex hooks")

    try:
        payload = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{CODEX_HOOKS.relative_to(REPO_ROOT)} is invalid JSON: {exc}")

    missing: list[str] = []
    referenced = 0
    for event, command in _codex_commands(payload):
        assert "${CLAUDE_PROJECT_DIR" not in command, (
            f"{event} still contains Claude's project-dir placeholder: {command}"
        )
        for relative in _codex_command_paths(command):
            referenced += 1
            if not (REPO_ROOT / relative).is_file():
                missing.append(f"{event}: {relative}")

    assert referenced, ".codex/hooks.json contains no repo-root handler paths to validate"
    assert not missing, "generated Codex hook handler(s) are missing:\n  " + "\n  ".join(missing)


# --- the instruction tier -----------------------------------------------------
# The vendored prose is drift-checked by `sync-devkit.py --check` like any other
# MANIFEST file. What that cannot see is a CLAUDE.md that *restates* the vendored
# policy instead of pointing at it: the copy is not in the MANIFEST, so it drifts
# freely while looking every bit as authoritative. That is the exact failure being
# undone here -- the policy lived inline in each repo, was copied forward by hand, and
# devkit's template had already lost a clause of the testing mandate.

VENDORED_POLICY = ".claude/rules/engineering.md"

# Sentences that belong to the vendored policy. Matching is on the distinctive middle
# of each clause, not the whole sentence, so a reworded copy is still caught -- a
# verbatim-only check would pass the moment someone paraphrased, which is precisely how
# the original drift happened.
POLICY_CLAUSES = (
    "gaps are not acceptable",
    "fail if the changed behavior were reverted",
    "never lower it merely to make a change pass",
    "silently work around a bad instruction",
)


def _instruction_files() -> list[Path]:
    """Every CLAUDE.md in the repo, skipping generated and vendor trees."""
    skip = ("node_modules", ".venv", "templates")
    return [
        p
        for p in REPO_ROOT.rglob("CLAUDE.md")
        if not any(part in skip for part in p.relative_to(REPO_ROOT).parts)
    ]


def _skill_files() -> list[Path]:
    root = REPO_ROOT / ".claude" / "skills"
    return sorted(root.glob("*/SKILL.md")) if root.is_dir() else []


def _rule_files() -> list[Path]:
    root = REPO_ROOT / ".claude" / "rules"
    return sorted(root.rglob("*.md")) if root.is_dir() else []


def _frontmatter(text: str) -> tuple[dict[str, str | list[str]], list[str]]:
    """Parse the deliberately small YAML shape instruction frontmatter allows."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return {}, ["must start with YAML frontmatter (`---`)"]
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, ["frontmatter has no closing `---`"]

    data: dict[str, str | list[str]] = {}
    problems: list[str] = []
    active_list: str | None = None
    for number, line in enumerate(lines[1:end], start=2):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        item = re.fullmatch(r"\s+-\s+(.+)", line)
        if item and active_list is not None:
            value = item.group(1).strip()
            # Bound to a local first: mypy narrows a name, never a subscript, so
            # asserting on `data[active_list]` leaves it `str | list[str]`.
            bucket = data[active_list]
            assert isinstance(bucket, list)
            bucket.append(value)
            continue
        field = re.fullmatch(r"([A-Za-z][\w-]*):(?:\s*(.*))?", line)
        if not field:
            problems.append(f"line {number} is outside the supported frontmatter shape")
            active_list = None
            continue
        key, value = field.groups()
        if key in data:
            problems.append(f"line {number} duplicates `{key}`")
            active_list = None
            continue
        if value:
            data[key] = value.strip()
            active_list = None
        else:
            data[key] = []
            active_list = key
    return data, problems


def _frontmatter_problems(path: Path, required_scalars: tuple[str, ...]) -> list[str]:
    data, problems = _frontmatter(path.read_text(encoding="utf-8"))
    for key in required_scalars:
        value = data.get(key)
        if not isinstance(value, str) or not value.strip(" '\""):
            problems.append(f"`{key}` must be a non-empty scalar")
    if "paths" in data:
        paths = data["paths"]
        if (
            not isinstance(paths, list)
            or not paths
            or any(not value.strip(" '\"") for value in paths)
        ):
            problems.append("`paths` must be a non-empty list of non-empty globs")
    return problems


_MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]*]\(<*([^)>\s]+)>*(?:\s+['\"][^)]*)?\)")


def _local_markdown_references(path: Path) -> list[Path]:
    references: list[Path] = []
    for target in _MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        clean = target.split("#", 1)[0]
        if not clean or "://" in clean or clean.startswith(("#", "/")):
            continue
        candidate = path.parent / clean
        if candidate.suffix.lower() == ".md":
            references.append(candidate.resolve())
    return references


def _backslash_path_fragments(text: str) -> list[str]:
    candidates = (
        re.findall(r"`([^`\n]+)`", text)
        + _MARKDOWN_LINK.findall(text)
        + re.findall(r"^\s*(?:-\s+|[\w-]+:\s+)(\S*\\\S*)", text, flags=re.MULTILINE)
    )
    return [value for value in candidates if re.search(r"[\w.*-]\\[\w.*-]", value)]


def _has_linked_table_of_contents(text: str) -> bool:
    toc = re.search(
        r"^## Table of contents\s*$([\s\S]*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE,
    )
    return bool(toc and re.search(r"]\(#[^)]+\)", toc.group(1)))


def test_instruction_files_stay_under_500_lines():
    """Large instruction blobs need decomposition, not an on-demand audit command."""
    for path in _instruction_files() + _rule_files() + _skill_files():
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines < 500, (
            f"{path.relative_to(REPO_ROOT)} is {lines} lines; split task workflows into "
            "skills and move detailed skill material into referenced support files"
        )


def test_rule_frontmatter_has_required_fields():
    for path in _rule_files():
        assert not (problems := _frontmatter_problems(path, ("description",))), (
            f"{path.relative_to(REPO_ROOT)}: {'; '.join(problems)}"
        )


def test_skill_frontmatter_has_required_fields():
    for path in _skill_files():
        assert not (problems := _frontmatter_problems(path, ("name", "description"))), (
            f"{path.relative_to(REPO_ROOT)}: {'; '.join(problems)}"
        )


def test_skill_references_are_one_level_deep_and_exist():
    for skill in _skill_files():
        for reference in _local_markdown_references(skill):
            assert reference.parent == skill.parent.resolve(), (
                f"{skill.relative_to(REPO_ROOT)} links to non-sibling reference {reference}"
            )
            assert reference.is_file(), (
                f"{skill.relative_to(REPO_ROOT)} links to missing reference {reference.name}"
            )
            nested = _local_markdown_references(reference)
            assert not nested, (
                f"{reference.relative_to(REPO_ROOT)} links to another local Markdown file; "
                "skill references must stay one level deep"
            )


def test_long_skill_references_have_a_linked_table_of_contents():
    for skill in _skill_files():
        for reference in _local_markdown_references(skill):
            text = reference.read_text(encoding="utf-8")
            if len(text.splitlines()) <= 100:
                continue
            assert _has_linked_table_of_contents(text), (
                f"{reference.relative_to(REPO_ROOT)} exceeds 100 lines and needs a "
                "linked `## Table of contents` section"
            )


def test_instruction_paths_use_forward_slashes():
    for path in _rule_files() + _skill_files():
        fragments = _backslash_path_fragments(path.read_text(encoding="utf-8"))
        assert not fragments, (
            f"{path.relative_to(REPO_ROOT)} uses backslashes in path-like text: {fragments}"
        )


def test_frontmatter_validator_rejects_missing_and_malformed_fields(tmp_path):
    missing = tmp_path / "missing.md"
    missing.write_text("# no frontmatter\n", encoding="utf-8")
    assert "must start" in " ".join(_frontmatter_problems(missing, ("description",)))

    malformed = tmp_path / "malformed.md"
    malformed.write_text("---\ndescription:\npaths: []\n---\n", encoding="utf-8")
    problems = _frontmatter_problems(malformed, ("description",))
    assert any("description" in problem for problem in problems)
    assert any("paths" in problem for problem in problems)


def test_reference_validators_reject_nested_links_and_backslash_paths(tmp_path):
    skill = tmp_path / "SKILL.md"
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    skill.write_text("[first](first.md) and `scripts\\check.py`\n", encoding="utf-8")
    first.write_text("[second](second.md)\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    assert _local_markdown_references(skill) == [first.resolve()]
    assert _local_markdown_references(first) == [second.resolve()]
    assert _backslash_path_fragments(skill.read_text(encoding="utf-8")) == ["scripts\\check.py"]


def test_long_reference_validator_requires_a_linked_table_of_contents():
    assert not _has_linked_table_of_contents("# Reference\n" + "content\n" * 101)
    assert not _has_linked_table_of_contents("## Table of contents\n\nNo links yet.\n")
    assert _has_linked_table_of_contents(
        "## Table of contents\n\n- [Details](#details)\n\n## Details\n"
    )


def test_skill_script_dependencies_exist():
    """A skill command naming a missing local script is a guaranteed dead end."""
    for skill in _skill_files():
        text = skill.read_text(encoding="utf-8")
        for rel in re.findall(r"\bpython(?:3)?\s+([.\w/-]+\.py)\b", text):
            assert (REPO_ROOT / rel).is_file(), (
                f"{skill.relative_to(REPO_ROOT)} invokes missing {rel}"
            )


@consumes_harness
def test_vendored_policy_is_present():
    """The rule every project's CLAUDE.md defers to has to actually be there.

    A dangling pointer is worse than a restatement: the CLAUDE.md says the authority
    lives elsewhere, and there is no elsewhere, so the policy silently applies nowhere.
    """
    assert (REPO_ROOT / VENDORED_POLICY).is_file(), (
        f"{VENDORED_POLICY} is missing -- run `python scripts/sync-devkit.py --pull`"
    )


@consumes_harness
def test_claude_md_defers_to_the_vendored_policy_rather_than_restating_it():
    """A CLAUDE.md that restates vendored policy has forked it.

    The copy reads as authoritative, is not in the MANIFEST, and so is not drift-checked
    -- the two diverge the first time either is edited, and the version an agent actually
    follows is whichever it happened to load. Projects add their own specifics (fixtures,
    isolation rules, what to mock); they cite the shared clauses.
    """
    policy_text = (REPO_ROOT / VENDORED_POLICY).read_text(encoding="utf-8")
    for path in _instruction_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT)
        for clause in POLICY_CLAUSES:
            assert clause in policy_text, f"stale POLICY_CLAUSES entry: {clause!r}"
            assert clause not in text, (
                f"{rel} restates vendored policy ({clause!r}). Cite "
                f"{VENDORED_POLICY} instead -- a second copy is not drift-checked."
            )


@consumes_harness
def test_vendored_skills_are_not_locally_edited():
    """Vendored skills carry no project's default branch, paths, or service names.

    `sync-devkit.py --check` already enforces this byte-for-byte, so this is the
    cheaper signal that says *why* when it trips: `ship` previously named `master`
    while `task_branch.detect_default_branch()` resolved the real branch at runtime.
    """
    skills = REPO_ROOT / ".claude" / "skills"
    if not skills.is_dir():
        pytest.skip("no vendored skills")
    vendored = {"ship"}
    for name in sorted(vendored):
        skill = skills / name / "SKILL.md"
        if not skill.is_file():
            continue
        text = skill.read_text(encoding="utf-8")
        assert "master" not in text, (
            f"{skill.relative_to(REPO_ROOT)} names a specific default branch; the "
            "vendored copy must defer to the one detect_default_branch() resolves"
        )
