---
description: Baseline engineering policy shared by every devkit project — test coverage, script conventions, the vendored harness seam, and the instruction-feedback loop
---

# Rule: Baseline engineering policy

Deliberately **unscoped** (no `paths:`) — this is the small set of rules that hold
everywhere, so there is no glob that should exempt a file from them.

**This file is vendored from devkit and is byte-identical in every project.** It is in
`sync-devkit.py`'s `MANIFEST`, so a local edit is reported as drift by the PR gate
rather than quietly becoming this project's private opinion. That is the point: these
paragraphs previously lived inline in each repo's `CLAUDE.md`, were copied forward by
hand, and drifted — devkit's own template had already lost a clause carameli still
had. To change the policy, change it here and let projects `--pull`.

A project's `CLAUDE.md` should **point at this file, not restate it.** A restatement is
a fork: it looks authoritative, it is not gated, and the two copies disagree the first
time either is edited.

## Testing

Every code change must include tests in the same commit. Every endpoint and every
testable unit of logic must have test coverage — gaps are not acceptable. If you add
or touch something that has no test, write the test in the same commit even if the
logic itself didn't change.

- **New unit of logic:** cover the happy path, the error cases, and the edge cases.
- **Bug fix:** write the regression test first, and watch it fail before you fix it. A
  regression test that has never failed is asserting the wrong thing.
- **Reversion check:** before declaring a change complete, identify which test would
  fail if the changed behavior were reverted. If no test would fail, the behavior is
  not covered yet.
- **Coverage floors are ratchets:** when a project enforces a minimum coverage floor,
  never lower it merely to make a change pass.
- **Run targeted tests** to verify a change — the module you touched — plus the
  linter. Leave full-suite runs to CI: they are slow, and a fresh-venv full run
  surfaces version-skew failures that have nothing to do with your change.
- **Fix failures in the code, not in the assertion.** Relaxing an assertion to get
  green deletes the only evidence that something is wrong.
- A skipped or `xfail` test carries a linked issue or a one-line reason in the marker.

> If the local toolchain or stack isn't available, still write the required tests in
> the same change and leave execution to CI. "I couldn't run it" is a reason to defer
> the run, never a reason to skip writing it.

Instruction files — `CLAUDE.md`, `.claude/rules/*`, `.claude/skills/*` — are covered by
this same mandate. See `.claude/rules/authoring.md`.

## Scripts

All scripts under `scripts/` are Python, for cross-environment compatibility (a local
desktop and a CI runner are rarely the same OS).

- **Expose pure importable functions** guarded by `if __name__ == '__main__'`, so the
  logic can be tested without spawning a subprocess.
- **Every new script ships with its tests in the same change.**
- **Hook scripts (`scripts/hooks/`) are stdlib only** — no third-party imports. Hooks
  run before the virtualenv is active, so an import of anything installed is a crash
  in the one context that cannot report it well.
- **Side effects live behind `main()`**, never at import time: the test suite imports
  these modules.

### Failure artifacts — fix from a file, not from the terminal

Any task or script whose failures an agent is expected to act on must persist those
failures to a **parseable artifact file** under `logs/`. Never rely on streamed
terminal output — it scrolls away and buries the signal. Keep the terminal to a status
line plus the artifact path, and put everything needed to diagnose in the file. Write
the artifact on failure too, not only on success, and overwrite it per run.

## Lint policy

### What is on, and why

Lint exists to catch **correctness and security** problems — the ones a human reviewer
reads past. Style and formatting are not judgement calls worth an agent's turn: a
formatter settles them, in place, with no discussion.

- **On:** correctness (undefined names, unreachable code, shadowed builtins, mutable
  default arguments), security (injection sinks, unsafe deserialisation, hard-coded
  secrets), and resource-handling (unclosed files, bare `except`).
- **Off:** anything a formatter can decide. `ruff format` runs on every edit via the
  `lint-fix.py` PostToolUse hook and again in CI, so line length, quote style and
  import order never reach a review.

The split has a practical consequence worth stating: a lint rule that fires on
something a formatter would fix is misconfigured, not useful. Turn it off rather than
teaching everyone to ignore it.

### Never silence a finding without naming the reason

`# noqa`, `# type: ignore`, `# nosec`, `eslint-disable` — each one is a claim that the
tool is wrong *here*. Write the claim down:

```python
result = subprocess.run(cmd, shell=True)  # noqa: S602 - agent-supplied tooling, not input
```

A bare `# noqa` is indistinguishable from a bare "I gave up", and the next agent
cannot tell which it was. Prefer the rule-specific form (`# noqa: S602`, not `# noqa`)
so the suppression stops applying the moment a *different* problem appears on that
line.

### When a linter is wrong: fix the producer, or escalate

There is no third option, and in particular **skipping is not one**.

1. **Fix the producer.** The finding is usually right about something even when it is
   wrong about the fix. Change the code so the rule has nothing to say.
2. **Suppress narrowly, with the reason**, per the section above — when the rule is
   genuinely inapplicable to this line.
3. **Report to the user with concrete options** — when neither of the above is honest.
   Say what the rule wants, why it does not fit, and what the alternatives cost.

**Never skip a failing check, and never describe an error as "cosmetic", "harmless",
or "pre-existing" to justify leaving it.** An error message is either actionable or it
is noise that must be removed at the source; deciding it is ignorable is the one move
that is always wrong, because it trains everyone downstream to ignore the next one too.
The same applies to tests: a failing test gets fixed or reported, never `skip`ped,
`xfail`ed, or deleted to make a run green.

If a check is genuinely obsolete, delete the check — deliberately, in its own change,
with the reason in the commit message. That is a different act from ignoring it.

## The vendored agent harness

The hook scripts, this rule, and the shared skills are **vendored from devkit, which is
the source of truth**. Each project commits its own copy, so a fresh clone gets
everything with no submodule and no install step.

- Everything project-specific lives in `.devkit.toml`, read by
  `scripts/hooks/harness_config.py`. **Never hard-code project specifics in a vendored
  file**: a new behaviour gets a manifest field and a default, not an `if project ==`
  branch, and not a paragraph that names one repo's paths.
- `python scripts/sync-devkit.py --check` fails on drift, `--pull` adopts upstream,
  `--push` sends a change authored here back up. `DEVKIT_VERSION` records which
  upstream commit the vendored copy corresponds to.
- **Every mode no-ops clean (exit 0) when `$DEVKIT_DIR` is unset.** That is
  correct before adoption and a trap after: if `--check` ever prints "nothing to do
  (skipping)" in CI, the gate is inert — fix the wiring, don't ignore it.
- A vendored script may depend on a file the project owns (`lint-all.py`,
  `run-tests.py`). Those dependencies are asserted by
  `scripts/hooks/tests/test_repo_contract.py`, because at runtime a missing one is a
  silent skip by design — the gate reports green having run nothing.

## Guardrail: the instruction-file feedback loop

If an instruction in a skill, a rule, or a `CLAUDE.md` sent you into a dead end or a
wasted operation — or a mistake you made would have been prevented by one that isn't
there — flag it in your report with the file, the line, and a proposed edit.

**Never silently work around a bad instruction.** Working around it fixes your current
turn and leaves the next agent to hit the same wall; the instruction files only improve
if the failures they cause are reported as defects in them.
