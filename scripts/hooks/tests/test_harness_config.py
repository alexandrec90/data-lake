"""Unit tests for the per-project harness config loader.

Covers the pure `from_dict` mapping and its tolerance of malformed/partial
manifests, plus a contract test that the committed `.devkit.toml` is
internally coherent (so a manifest edit that would silently change harness
behaviour fails here instead of at runtime).

**This file is vendored into every consuming project**, so nothing here may assert
a value that is specific to one project. It previously pinned carameli's literal
credentials, paths, and skill list, which made the whole vendored suite red in any
repo whose manifest differed — the exact opposite of the portability the harness
exists for. Assert *invariants* of a manifest, never one project's contents.
"""

from pathlib import Path

from conftest import REPO_ROOT, load_module

cfg = load_module("scripts/hooks/harness_config.py")


def test_defaults_are_a_minimal_neutral_harness():
    c = cfg.Config()
    assert c.env_prefix == "DEVKIT"
    assert c.db.enabled is False
    assert c.frontend.enabled is False
    # Neutral defaults describe a generic Python project, not carameli.
    assert c.app_dir == "app/" and c.unit_tests == "tests/unit"


def test_env_prefixes_control_vars():
    c = cfg.Config(env_prefix="CARAMELI")
    assert c.env("SKIP_STOP_VERIFY") == "CARAMELI_SKIP_STOP_VERIFY"
    assert c.env("STOP_TESTS_AUTOSTART") == "CARAMELI_STOP_TESTS_AUTOSTART"


def test_from_dict_empty_is_defaults():
    assert cfg.from_dict({}) == cfg.Config()


def test_from_dict_maps_full_manifest():
    c = cfg.from_dict(
        {
            "project": {"env_prefix": "FOO"},
            "paths": {"app": "src/", "tests": "t/", "unit_tests": "t/unit"},
            "db": {
                "enabled": True,
                "services": ["db", "cache"],
                "db_service": "db",
                "db_port": 6000,
                "redis_service": "cache",
                "redis_port": 7000,
                "user": "u",
                "password": "p",
                "name": "n",
                "url_scheme": "postgresql+asyncpg",
                "url_env": ["DATABASE_URL", "DIRECT_DATABASE_URL"],
                "redis_env": "CACHE_URL",
                "test_env": {"API_KEY_SECRET": "k"},
            },
            "frontend": {"enabled": True, "dir": "web", "src": "web/src/"},
        }
    )
    assert c.env_prefix == "FOO"
    assert c.app_dir == "src/" and c.unit_tests == "t/unit"
    assert c.db.enabled is True
    assert c.db.services == ("db", "cache")
    assert c.db.db_port == 6000 and c.db.redis_service == "cache"
    assert c.db.url_env == ("DATABASE_URL", "DIRECT_DATABASE_URL")
    assert c.db.redis_env == "CACHE_URL"
    assert c.db.test_env == {"API_KEY_SECRET": "k"}
    assert c.frontend.enabled is True and c.frontend.dir == "web"


def test_from_dict_tolerates_wrong_types():
    # Non-dict sections and wrong-typed fields fall back to defaults, never raise.
    c = cfg.from_dict(
        {
            "project": "nope",
            "db": {"services": "not-a-list"},
            "frontend": [1, 2],
        }
    )
    assert c.env_prefix == "DEVKIT"
    assert c.db.services == ("db", "redis")  # bad `services` -> default
    assert c.frontend.enabled is False


def test_load_missing_manifest_returns_defaults(tmp_path: Path):
    assert cfg.load(tmp_path) == cfg.Config()


def test_load_parses_real_toml(tmp_path: Path):
    (tmp_path / cfg.MANIFEST_NAME).write_text(
        '[project]\nenv_prefix = "ZED"\n[db]\nenabled = true\nservices = ["db"]\n'
    )
    c = cfg.load(tmp_path)
    assert c.env_prefix == "ZED"
    assert c.db.enabled is True and c.db.services == ("db",)


def test_load_malformed_toml_returns_defaults(tmp_path: Path):
    (tmp_path / cfg.MANIFEST_NAME).write_text("this is = = not valid toml [[[")
    assert cfg.load(tmp_path) == cfg.Config()


# --- contract: the committed manifest reproduces carameli's constants ---------


def test_repo_manifest_loads_and_is_coherent():
    """This repo's own `.devkit.toml` must load and hold together.

    Portable replacement for a test that pinned carameli's literal values. It still
    catches the failures that matter — a manifest that no longer parses, or a
    half-filled block — without asserting anything project-specific.
    """
    c = cfg.load(REPO_ROOT)

    # A blank prefix would make every control var `_SKIP_STOP_VERIFY`, colliding
    # across projects on one machine.
    assert c.env_prefix and c.env_prefix.isupper()
    assert c.env("SKIP_STOP_VERIFY").startswith(f"{c.env_prefix}_")

    # Directory-ish fields must be usable as path prefixes: `stop.py` decides which
    # checks to run with `path.startswith(CFG.app_dir)`, so a missing trailing slash
    # makes `apps/` match `app/` and silently widens the test selection.
    assert c.app_dir.endswith("/")
    assert c.tests_dir.endswith("/")
    assert c.unit_tests

    if c.db.enabled:
        # A half-filled DB block yields a URL like `postgresql://:@host:5432/`,
        # which fails at connect time with a message that points nowhere.
        assert c.db.user and c.db.name and c.db.password
        assert c.db.url_scheme and c.db.url_env
        assert c.db.db_service in c.db.services

    if c.frontend.enabled:
        assert c.frontend.dir and c.frontend.test_cmd
        # `src` gates the vitest tier by prefix match, same trailing-slash logic.
        assert c.frontend.src.endswith("/")


# --- [python] install_command: the dependency-model escape hatch ---------------
# session-start.sh normally *detects* the model from files on disk; this field is
# only for a project that fits none of the known shapes. Its job is to be absent
# (and harmless) far more often than it is set.


def test_python_install_command_defaults_to_empty_so_detection_wins():
    assert cfg.Config().python.install_command == ""
    assert cfg.from_dict({}).python.install_command == ""


def test_python_install_command_is_read_from_the_manifest():
    c = cfg.from_dict({"python": {"install_command": "make deps"}})
    assert c.python.install_command == "make deps"


def test_malformed_python_section_degrades_to_default():
    # Same never-raises contract as every other section: a typo must not break
    # provisioning, it must fall back to detection.
    for bad in ({"python": "uv sync"}, {"python": []}, {"python": {"install_command": 7}}):
        assert isinstance(cfg.from_dict(bad).python.install_command, str)


# --- [bash]: the PreToolUse output cap ----------------------------------------
# Read by both enforce-capped-bash.py (the number it quotes when blocking) and
# invoke-capped.py (the cap it actually applies). They must agree, which is why
# there is one field and not two constants.


def test_bash_defaults_are_a_usable_cap():
    assert cfg.Config().bash.max_bytes == 4000
    assert cfg.Config().bash.head_bytes == 2000
    # head must fit inside the cap or the tail window is negative.
    assert cfg.Config().bash.head_bytes <= cfg.Config().bash.max_bytes


def test_bash_values_are_read_from_the_manifest():
    c = cfg.from_dict({"bash": {"max_bytes": 12000, "head_bytes": 3000}})
    assert c.bash.max_bytes == 12000
    assert c.bash.head_bytes == 3000


def test_bash_partial_block_keeps_the_other_default():
    c = cfg.from_dict({"bash": {"max_bytes": 8000}})
    assert c.bash.max_bytes == 8000
    assert c.bash.head_bytes == 2000


def test_malformed_bash_section_degrades_to_defaults():
    # Same never-raises contract as every other section.
    for bad in ({"bash": "4000"}, {"bash": []}, {"bash": {"max_bytes": "wide"}}):
        c = cfg.from_dict(bad)
        assert c.bash.max_bytes == 4000


def test_bash_rejects_bool_which_is_an_int_subclass():
    """`max_bytes = true` must not silently become a 1-byte cap.

    bool is a subclass of int, so a naive int() accepts it and every Bash call
    would come back as a truncation marker with no output.
    """
    c = cfg.from_dict({"bash": {"max_bytes": True}})
    assert c.bash.max_bytes == 4000


def test_bash_accepts_a_numeric_string():
    # TOML gives an int, but a hand-edited manifest may quote it.
    assert cfg.from_dict({"bash": {"max_bytes": "9000"}}).bash.max_bytes == 9000


def test_lookup_returns_scalars_and_empty_for_anything_else():
    c = cfg.from_dict({"python": {"install_command": "uv sync"}, "project": {"env_prefix": "X"}})
    assert cfg.lookup(c, "python.install_command") == "uv sync"
    assert cfg.lookup(c, "env_prefix") == "X"
    # Booleans come out lowercase: the only consumer is shell, which wants
    # `[ "$v" = "true" ]`, not Python's "True".
    assert cfg.lookup(c, "db.enabled") == "false"
    assert cfg.lookup(cfg.from_dict({"db": {"enabled": True}}), "db.enabled") == "true"
    # Unknown path and non-scalar both yield "" so a shell caller can use one
    # `[ -n "$v" ]` test for "no value" and "no such field" alike.
    assert cfg.lookup(c, "nope") == ""
    assert cfg.lookup(c, "python.nope.deeper") == ""


def test_cli_prints_the_value_for_shell_callers(tmp_path):
    """session-start.sh shells out to this; it must exit 0 and print a bare value."""
    import subprocess
    import sys

    (tmp_path / ".devkit.toml").write_text(
        '[python]\ninstall_command = "poetry install --with dev"\n', encoding="utf-8"
    )
    script = REPO_ROOT / "scripts" / "hooks" / "harness_config.py"
    run = subprocess.run(
        [sys.executable, str(script), "python.install_command"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0
    assert run.stdout.strip() == "poetry install --with dev"


def test_cli_exits_zero_with_no_manifest_and_no_args(tmp_path):
    # A hook must never die over config. No manifest, no argv -> empty line, rc 0.
    import subprocess
    import sys

    script = REPO_ROOT / "scripts" / "hooks" / "harness_config.py"
    for argv in ([], ["python.install_command"], ["totally.unknown"]):
        run = subprocess.run(
            [sys.executable, str(script), *argv], cwd=tmp_path, capture_output=True, text=True
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == ""


# --- [docker]: the opt-in the workspace's stop-idle pass reads ------------------


def test_docker_auto_stop_defaults_off():
    """Opt-in: the key licenses stopping this project's stack unattended, so absence
    -- of the key, the table, or the whole manifest -- must read as "keep it up"."""
    assert cfg.Config().docker.auto_stop is False
    assert cfg.from_dict({}).docker.auto_stop is False
    assert cfg.from_dict({"docker": {}}).docker.auto_stop is False


def test_docker_auto_stop_requires_the_literal_true():
    """`1`, `"yes"` and `"true"` are typos, and a typo in a key that licenses stopping
    a stack must land on the safe side."""
    assert cfg.from_dict({"docker": {"auto_stop": True}}).docker.auto_stop is True
    for junk in ("yes", "true", 1, [True], None):
        assert cfg.from_dict({"docker": {"auto_stop": junk}}).docker.auto_stop is False


def test_a_docker_table_of_the_wrong_shape_is_ignored():
    assert cfg.from_dict({"docker": "auto_stop"}).docker.auto_stop is False
