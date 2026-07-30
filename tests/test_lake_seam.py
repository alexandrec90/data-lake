"""The boundary that justifies this package existing separately.

The lake is shared: whatever tables it declares, every consumer of the package gets. Account-
shaped data — orders, executions, predictions, risk state — is the opposite of shareable, and
a consumer's audit trail leaking into a package a *second* consumer imports is the specific
failure this guards against.

The rule is therefore not "these tables are fine" but "nothing account-shaped is ever declared
here", enforced structurally so it fails when someone adds such a table rather than when
someone notices months later.
"""

import ast
import pathlib

import pytest

from data_lake.archive.catalog import DATASET_SPECS
from data_lake.db import models
from data_lake.db.base import Base

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "data_lake"

#: Table names that belong to a consumer, never to the shared package. Spelled out literally
#: so this file cannot be defeated by editing a list somewhere else.
NEVER_SHAREABLE = {
    "orders",
    "executions",
    "strategy_snapshots",
    "predictions",
    "backtest_runs",
}

#: Column-name fragments that mark account/audit data. A new lake table tripping one of these
#: is not necessarily wrong, but it must be looked at — hence the explicit allow-list below.
ACCOUNT_SHAPED = ("order_", "_order", "execution", "commission", "account_", "perm_id")


def _lake_tables() -> set[str]:
    return set(Base.metadata.tables)


def test_no_account_table_is_declared_in_the_package():
    assert NEVER_SHAREABLE & _lake_tables() == set()


def test_every_mapped_class_lives_in_the_models_module():
    """One place to look for "what does the lake hold" — and one place to review changes."""
    declared = {
        obj.__tablename__
        for obj in vars(models).values()
        if isinstance(obj, type) and issubclass(obj, Base) and obj is not Base
    }
    assert declared == _lake_tables()


@pytest.mark.parametrize("table_name", sorted(Base.metadata.tables))
def test_lake_tables_carry_no_account_shaped_columns(table_name):
    """Catches the subtler leak: a shareable table growing an ``account_id``/``order_id``."""
    columns = {column.name for column in Base.metadata.tables[table_name].columns}
    suspicious = {column for column in columns if any(mark in column for mark in ACCOUNT_SHAPED)}
    assert suspicious == set(), (
        f"{table_name} has account-shaped column(s) {sorted(suspicious)} — account data "
        "belongs in the consumer's own tables, not the shared lake"
    )


def test_models_only_depends_on_the_shared_base():
    """What keeps the schema half portable: no imports beyond ``db.base`` and SQLAlchemy."""
    tree = ast.parse((SRC / "db" / "models.py").read_text(encoding="utf-8"))
    internal = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("data_lake")
    }
    assert internal == {"data_lake.db.base"}


@pytest.mark.parametrize("spec", DATASET_SPECS, ids=lambda s: s.name)
def test_archive_only_publishes_lake_datasets(spec):
    """Nothing written to the shared bucket may correspond to a table the lake doesn't own."""
    assert spec.name in _lake_tables()
    assert spec.name not in NEVER_SHAREABLE


def test_package_never_imports_a_consumer():
    """The dependency runs consumer -> lake. The reverse would un-share the package."""
    offenders = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        if any(name.startswith("ibkr_trader") for name in names):
            offenders.append(path.relative_to(SRC).as_posix())
    assert offenders == []
