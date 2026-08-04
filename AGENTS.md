# data-lake

Shared ingestion + cold-storage layer, extracted from `ibkr_trader` so more than one
project can reuse the same connectors, schema, and storage architecture without
duplicating them. Installed by consumers as an editable path dependency from a sibling
checkout.

## Baseline policy

`.claude/rules/engineering.md` (testing, script conventions, failure artifacts, the
harness seam, the instruction-feedback loop) and `.claude/rules/authoring.md` apply here
and are **vendored from devkit** — they are byte-identical in every project and gated by
`scripts/sync-devkit.py --check`. Do not restate them here; a restatement is a fork that
looks authoritative and is not gated.

Everything below is what is true about this package specifically.

## The one rule that shapes everything else

**The dependency runs consumer → lake, and never the reverse.** Nothing under
`src/data_lake/` may import `ibkr_trader` or any other consumer.

This is not a style preference — it is what makes the package shareable at all. An
import back into a consumer would mean the *second* project to adopt this package pulls
in the first one's code. `tests/test_lake_seam.py::test_package_never_imports_a_consumer`
walks every module's AST and enforces it, so a violation fails fast and by filename
rather than at some consumer's import time.

Two consequences worth stating, because they are where the rule actually bites:

- **A job or helper that needs a consumer's layer stays in the consumer.** Not "gets
  moved and then imported back" — stays. If the thing you are extracting reaches into
  the consumer's signals, models, or maintenance code, that part is not shareable and
  splitting it is the work.
- **Anything this package needs from a consumer arrives through the seam**, never
  through an import. See below.

## The composition root

This package owns **no config system, no SQLAlchemy engine, and no migrations**. A
consumer supplies all three, once, at its entry point:

```python
data_lake.configure(settings=get_settings(), session_factory=get_session)
```

`src/data_lake/runtime.py` is that seam and its docstring is the full explanation.
`src/data_lake/settings.py` declares — as `Protocol`s — exactly what the package reads
from a consumer's settings object. Structural typing, so a consumer's existing pydantic
`BaseSettings` satisfies it with nothing to inherit and no import back into its config
module.

### Adding a capability means extending the Protocol first

This is the step that is easy to miss, and it is usually most of the work.

`LakeSettings` describes what the package reads *today* — provider credentials and
archive location. It does **not** carry scheduling cadences, universe-file paths, or
anything else a consumer happens to have. So porting any consumer code that reads
`settings.<something_not_in_the_Protocol>` means:

1. Add the members to the relevant `Protocol` in `settings.py` (or add a new one and
   fold it into `LakeSettings`).
2. Declare them as **read-only properties**, never plain attributes — a Protocol's
   attributes are invariant, so `backend: str` would reject a consumer whose field is
   typed `Literal[...]`. Properties are covariant, and an ordinary mutable attribute
   satisfies them.
3. Give `data_lake.testing.lake_settings()` a default for each, or every existing test
   that builds a settings stub starts failing on the missing field.

A capability whose config cannot be expressed as a Protocol over what consumers already
have is a sign it belongs in the consumer.

## What must never be declared here

Orders, executions, predictions, backtest runs, risk state — anything account-shaped.
Whatever this package declares, *every* consumer gets, so a table added here ships one
consumer's audit trail to the next. `tests/test_lake_seam.py` enforces this down to
catching a shareable table that grows an `account_id`, and separately holds the archive
to publishing only datasets backed by a lake table.

The consumer declares its private tables against the same `data_lake.db.base.Base`, so a
single `Base.metadata` still covers everything its Alembic needs to see.

## Privacy

Social payloads are scraped content held under Québec Law 25. **Authors are stored
hashed only** (`ingestion.base.stable_hash`) — never usernames — and the archive bucket
must be private. R2 buckets are private by default, but an enabled `r2.dev` managed
domain or a custom domain makes objects public, and S3 credentials cannot report that:
only the Cloudflare REST API or the dashboard can.

## Optional extras, guarded imports

`archive` (pyarrow + boto3) and `research` (duckdb) are optional dependencies, and the
modules behind them import cleanly without the extra installed — raising a clear install
hint only when a call actually needs Parquet or the DuckDB lens. Keep that pattern for
anything new that pulls a heavy dependency: importing the package must never require it.

```bash
uv sync                                    # library + dev tooling
uv sync --extra archive --extra research   # + Parquet/S3 + the DuckDB lens
```

## Commands

```bash
python scripts/run-tests.py            # the suite, failures -> logs/test-failures.log
python scripts/run-tests.py --changed  # pytest's last-failed subset
python scripts/lint-all.py             # ruff + format + mypy -> logs/lint-errors.log
python scripts/lint-all.py --changed   # ruff over the diff; mypy stays full-scope
```

These are the **contract entrypoints** the shared workspace tasks dispatch to through
devkit's `devkit_project.py`; keep them at these paths accepting these arguments.
Underneath they are `uv run`, because the caller is usually a VS Code task or a hook
launched with the desktop's PATH rather than this project's `.venv`.

The underlying tools directly, when you want them:

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src
```

## The two test trees

- **`tests/`** — this package's own. **No network, no real bucket, no database**:
  provider clients are faked, storage is a temp directory, and the session is in-memory
  SQLite injected per call. `tests/_lake_env.py` holds the one settings object the suite
  configures the package with — set a field with
  `monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")` rather than an environment
  variable, so a credential exported in your shell can never leak into a run.
  `tests/conftest.py` deliberately configures **settings but no session factory**, so
  anything that forgets to inject one fails loudly instead of reaching for a database.
- **`scripts/hooks/tests/`** — the vendored harness tier, shipped from devkit and
  excluded from `testpaths`. Run it with `python -m pytest scripts/hooks/tests/ -q`, or
  through the workspace's "Test: Harness Hook Tests" task. Never edit these here; a
  change belongs upstream in devkit and arrives via `--pull`.

Coverage is a ratchet (`fail_under` in `pyproject.toml`) — raise it as coverage grows,
never lower it to make a change pass.

## The vendored harness

`scripts/hooks/`, `.claude/rules/`, and the shared skills come from devkit and are
byte-compared against it. `DEVKIT_VERSION` records the release this copy corresponds to.

```bash
python scripts/sync-devkit.py --check   # drift (needs $DEVKIT_DIR)
python scripts/sync-devkit.py --pull    # adopt upstream
```

Every mode no-ops clean when `$DEVKIT_DIR` is unset — correct before adoption, a trap
after. If `--check` prints "nothing to do (skipping)" in CI, the gate is inert; fix the
wiring rather than ignoring it.
