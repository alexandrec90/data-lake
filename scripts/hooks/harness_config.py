#!/usr/bin/env python3
"""Per-project configuration for the shared agent-harness hook scripts.

The hook scripts (`stop.py`, and later the rest of `scripts/hooks/`) are meant to
be **vendored unchanged into every project**. Everything that differs between
projects -- the control-env prefix, the DB credentials/ports/service names, the
frontend layout, and the source-tree shape --
lives here, read from a committed `.devkit.toml` at the repo root. The
scripts stay shape-agnostic; a new project drops in a manifest instead of forking
the code.

Design contract:
  - **stdlib only** (`tomllib`, 3.11+). Hooks run before the venv is active.
  - **Never raises.** A missing/unparseable manifest, or an interpreter without
    `tomllib`, falls back to `Config()` defaults -- a minimal but valid harness
    (lint + script-tests, no DB tier, no frontend). A config typo must never
    break the Stop hook.
  - **Neutral defaults.** Defaults describe a generic Python project, not
    carameli; carameli's specifics come from its own `.devkit.toml`.

Pure and unit-tested in `scripts/hooks/tests/test_harness_config.py`.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

MANIFEST_NAME = ".devkit.toml"


@dataclass(frozen=True)
class DbConfig:
    """The DB-backed test tier (Tier 2b). `enabled=False` skips the tier entirely."""

    enabled: bool = False
    services: tuple[str, ...] = ("db", "redis")  # what "reachable"/"up" means
    db_service: str = "db"
    db_port: int = 5432
    redis_service: str = "redis"
    redis_port: int = 6379
    user: str = ""
    password: str = ""
    name: str = ""
    url_scheme: str = "postgresql+asyncpg"
    # Env var names the DB URL is exposed under (carameli needs two).
    url_env: tuple[str, ...] = ("DATABASE_URL",)
    redis_env: str = "REDIS_URL"
    # Extra env for host pytest: name -> default (an already-set os.environ wins).
    test_env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontendConfig:
    """The frontend (vitest/tsc) tier. `enabled=False` skips all frontend checks."""

    enabled: bool = False
    dir: str = "frontend"
    src: str = "frontend/src/"  # prefix that gates the vitest tier
    skin: str = "frontend/src/skins"  # subtree whose change triggers a typecheck
    test_cmd: tuple[str, ...] = ("run", "test:run")
    typecheck_cmd: tuple[str, ...] = ("run", "typecheck")


@dataclass(frozen=True)
class BashConfig:
    """The PreToolUse Bash output cap (`enforce-capped-bash.py`).

    There is no `enabled` flag on purpose: wiring the hook in
    `.claude/settings.json` *is* the opt-in, the same way `lint-fix.py` works. A
    project that does not want the gate does not wire it.

    `head_bytes` is how much of the cap goes to the *start* of the output; the
    remainder is the tail. Both windows are kept because the two useful parts of a
    long command's output are the first lines (what it was doing) and the last
    (how it failed) -- the middle is what an agent can afford to lose.
    """

    max_bytes: int = 4000
    head_bytes: int = 2000


@dataclass(frozen=True)
class PythonConfig:
    """How to provision this project's Python toolchain.

    Deliberately just an escape hatch. `session-start.sh` *detects* the dependency
    model from the files on disk (`uv.lock` -> uv sync, `requirements-dev.txt` ->
    pip-tools locks, else `pyproject.toml`), because a lockfile cannot drift from
    reality the way a manifest field can. Set `install_command` only for a project
    that fits none of those shapes; it then wins over detection.
    """

    install_command: str = ""


@dataclass(frozen=True)
class Config:
    """Shape of the project the harness scripts operate on."""

    # Prefix for harness control env vars, e.g. "CARAMELI" ->
    # CARAMELI_SKIP_STOP_VERIFY / CARAMELI_STOP_TESTS_AUTOSTART / ...
    env_prefix: str = "DEVKIT"
    app_dir: str = "app/"
    tests_dir: str = "tests/"
    unit_tests: str = "tests/unit"
    db: DbConfig = field(default_factory=DbConfig)
    frontend: FrontendConfig = field(default_factory=FrontendConfig)
    python: PythonConfig = field(default_factory=PythonConfig)
    bash: BashConfig = field(default_factory=BashConfig)

    def env(self, suffix: str) -> str:
        """The prefixed control-env name, e.g. env("SKIP_STOP_VERIFY")."""
        return f"{self.env_prefix}_{suffix}"


def _as_str_tuple(value: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return fallback


def _db_from(raw: dict[str, Any], default: DbConfig) -> DbConfig:
    test_env = raw.get("test_env")
    return replace(
        default,
        enabled=bool(raw.get("enabled", default.enabled)),
        services=_as_str_tuple(raw.get("services"), default.services),
        db_service=str(raw.get("db_service", default.db_service)),
        db_port=int(raw.get("db_port", default.db_port)),
        redis_service=str(raw.get("redis_service", default.redis_service)),
        redis_port=int(raw.get("redis_port", default.redis_port)),
        user=str(raw.get("user", default.user)),
        password=str(raw.get("password", default.password)),
        name=str(raw.get("name", default.name)),
        url_scheme=str(raw.get("url_scheme", default.url_scheme)),
        url_env=_as_str_tuple(raw.get("url_env"), default.url_env),
        redis_env=str(raw.get("redis_env", default.redis_env)),
        test_env=(
            {str(k): str(v) for k, v in test_env.items()}
            if isinstance(test_env, dict)
            else dict(default.test_env)
        ),
    )


def _frontend_from(raw: dict[str, Any], default: FrontendConfig) -> FrontendConfig:
    return replace(
        default,
        enabled=bool(raw.get("enabled", default.enabled)),
        dir=str(raw.get("dir", default.dir)),
        src=str(raw.get("src", default.src)),
        skin=str(raw.get("skin", default.skin)),
        test_cmd=_as_str_tuple(raw.get("test_cmd"), default.test_cmd),
        typecheck_cmd=_as_str_tuple(raw.get("typecheck_cmd"), default.typecheck_cmd),
    )


def _python_from(raw: dict[str, Any], default: PythonConfig) -> PythonConfig:
    return replace(
        default, install_command=str(raw.get("install_command", default.install_command))
    )


def _int_or(value: Any, fallback: int) -> int:
    """int(value) when it is a real number, else `fallback`. Never raises.

    `bool` is excluded deliberately: it is an `int` subclass, so `max_bytes = true`
    would otherwise silently become a 1-byte cap.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _bash_from(raw: dict[str, Any], default: BashConfig) -> BashConfig:
    return replace(
        default,
        max_bytes=_int_or(raw.get("max_bytes"), default.max_bytes),
        head_bytes=_int_or(raw.get("head_bytes"), default.head_bytes),
    )


def from_dict(data: dict[str, Any]) -> Config:
    """Build a Config from an already-parsed manifest dict. Pure; never raises."""
    default = Config()
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    db_raw = data.get("db", {}) if isinstance(data.get("db"), dict) else {}
    fe_raw = data.get("frontend", {}) if isinstance(data.get("frontend"), dict) else {}
    py_raw = data.get("python", {}) if isinstance(data.get("python"), dict) else {}
    bash_raw = data.get("bash", {}) if isinstance(data.get("bash"), dict) else {}
    return Config(
        env_prefix=str(project.get("env_prefix", default.env_prefix)),
        app_dir=str(paths.get("app", default.app_dir)),
        tests_dir=str(paths.get("tests", default.tests_dir)),
        unit_tests=str(paths.get("unit_tests", default.unit_tests)),
        db=_db_from(db_raw, default.db),
        frontend=_frontend_from(fe_raw, default.frontend),
        python=_python_from(py_raw, default.python),
        bash=_bash_from(bash_raw, default.bash),
    )


def load(root: Path) -> Config:
    """Load `<root>/.devkit.toml`, or return defaults when absent/unreadable.

    Any failure -- no file, no `tomllib`, a parse error -- degrades to `Config()`
    so the harness stays a valid (if minimal) lint+script-test gate.
    """
    manifest = root / MANIFEST_NAME
    if not manifest.exists():
        return Config()
    try:
        import tomllib  # stdlib 3.11+; guarded for older shims
    except ModuleNotFoundError:
        return Config()
    with contextlib.suppress(OSError, ValueError), manifest.open("rb") as fh:
        return from_dict(tomllib.load(fh))
    return Config()


def lookup(cfg: Config, dotted: str) -> str:
    """One config value as a plain string, for shell callers. Never raises.

    An unknown or non-scalar path yields "" rather than an error, so a caller can
    treat "no value" and "no such field" the same way -- which is what a shell
    script wants: `[ -n "$value" ]`.
    """
    node: Any = cfg
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return ""
    if isinstance(node, bool):
        # Lowercase so the shell caller can write `[ "$v" = "true" ]` rather than
        # matching Python's "True" — this value only ever crosses into shell.
        return "true" if node else "false"
    return "" if isinstance(node, (dict, list, tuple)) else str(node)


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    # `python3 scripts/hooks/harness_config.py python.install_command` -> stdout.
    # Exists so `.claude/hooks/session-start.sh` can read the manifest without a
    # TOML parser in shell. Always exits 0: a hook must not die over config.
    import sys

    print(lookup(load(Path.cwd()), sys.argv[1]) if len(sys.argv) > 1 else "")
