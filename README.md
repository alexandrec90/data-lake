# data-lake

Shared ingestion + cold-storage layer, extracted from `ibkr_trader` so more than one project can
reuse the same data without duplicating connectors, schema, or storage architecture.

Two halves, usable independently:

- **`data_lake.ingestion`** — one connector per provider (news, social, market). Each fetches from
  its API and **upserts into the consumer's Postgres**, keyed so re-running is idempotent. Social
  posts are the exception: they come from the archive (below).
- **`data_lake.archive`** — offload cold rows to Parquet on any S3-compatible bucket (Cloudflare
  R2) with verify-before-delete, a self-describing catalog under `_catalog/`, and a read-only
  DuckDB lens.

The package owns **no config system, no engine, and no migrations**. The consumer owns all three.

## Install

```bash
uv sync                                    # library + dev tooling
uv sync --extra archive --extra research   # + Parquet/S3 + the DuckDB lens
```

## Wire it up

Once, at your application's entry point:

```python
import data_lake
from myapp.config import get_settings
from myapp.db import get_session

data_lake.configure(settings=get_settings(), session_factory=get_session)
```

- **`settings`** — any object carrying the attributes in `data_lake.settings.LakeSettings`. It is
  a `Protocol`, so this is structural: an existing pydantic `BaseSettings` with the right field
  names already satisfies it, with nothing to inherit and no import back into your config module.
- **`session_factory`** — any zero-arg callable returning a context manager that yields a
  SQLAlchemy `Session` and commits on clean exit.

Per-call injection always beats the configured default:

```python
FinnhubNewsConnector(settings=other, session_factory=other_factory).fetch(symbol="AAPL")
```

An unconfigured process raises a `RuntimeError` naming the fix rather than silently reaching for
a database. `data_lake.testing.lake_settings(**overrides)` gives tests a settings stub.

## Schema

`data_lake.db.models` declares the shareable tables — instruments, price bars, dividends, share
counts, fundamentals, earnings, features, news articles, social posts, trend points — against
`data_lake.db.base.Base`.

**The consumer owns its own migrations.** Declare your private tables against the same `Base` and
a single `Base.metadata` still covers everything Alembic needs to see.

### What must never be declared here

Orders, executions, predictions, backtest runs, risk state — anything account-shaped. Whatever
this package declares, *every* consumer of it gets, so a table added here would ship one
consumer's audit trail to the next. `tests/test_lake_seam.py` enforces that mechanically, down to
catching a shareable table that grows an `account_id`.

Two related guardrails, same file: nothing under `src/data_lake/` may import a consumer package
(the dependency runs consumer → lake, never the reverse), and the archive may only publish
datasets backed by a lake table.

## Social posts (Reddit, X)

There is no Reddit or X API connector — the PRAW one was retired because its API key could not
be obtained. The sibling `social-scraper` project collects posts keylessly
through a browser and exports them to the archive as the `social_scraper_posts` dataset
(`social_scraper/posts/platform=<p>/dt=<date>/part.parquet` plus `_catalog/social_scraper_posts.json`).
`SocialScraperConnector` (`data_lake.ingestion.social.social_scraper`) reads that dataset from the
configured archive store and upserts it into `social_posts` on `(platform, external_id)`, with
`sentiment` left NULL for the consumer to score:

```python
SocialScraperConnector().fetch()                     # every platform; returns rows upserted
SocialScraperConnector().fetch(platforms=["reddit"])
```

It needs the `archive` extra and an archive backend. It reads only partitions social-scraper has
rewritten since the newest `fetched_at` already loaded per platform, and it **raises** when the
archive holds no `social_scraper_posts` manifest — that means the export has never reached this
store, which a returned 0 would hide.

## Privacy

Social payloads are scraped content held under Québec Law 25: **authors are stored hashed only**
(`ingestion.base.stable_hash`), never usernames, and the bucket must be private. R2 buckets are
private by default, but an enabled `r2.dev` managed domain or a custom domain makes objects
public — and S3 credentials cannot report that, only the Cloudflare REST API or the dashboard can.

## Development

```bash
uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src
```

Tests never hit the network or a real bucket: provider clients are faked, storage is a temp
directory, and the database is in-memory SQLite. `tests/_lake_env.py` holds the one settings
object the suite configures the package with — set a field with
`monkeypatch.setattr(SETTINGS, "finnhub_key", "test-key")` rather than an environment variable,
so a credential exported in your shell can never leak into a test run.
