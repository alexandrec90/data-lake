"""Every news connector that keeps a provider payload in ``raw`` has declared who is in it.

News connectors keep ``raw`` whole so a later parser can reprocess it — which means whatever
a provider sends lands, unless the connector said otherwise up front. Law 25 requires people
be stored hashed only, so a news connector writing ``raw`` must declare ``PERSON_FIELDS``:
the fields ``ingestion.base.pseudonymize`` hashes, declared even when the provider sends none
today, so a future byline is hashed on arrival.

Found structurally, so a new news connector is held to this the moment it writes ``raw``.
"""

import ast
import importlib
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "data_lake"
NEWS = SRC / "ingestion" / "news"


def _writes_raw(tree: ast.AST) -> bool:
    """``Model(raw=...)`` or ``row.raw = ...`` anywhere in the module."""
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "raw":
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Attribute) and target.attr == "raw" for target in node.targets
        ):
            return True
    return False


def _raw_writing_modules() -> list[str]:
    modules = []
    for path in sorted(NEWS.rglob("*.py")):
        if _writes_raw(ast.parse(path.read_text(encoding="utf-8"))):
            relative = path.relative_to(SRC.parent).with_suffix("")
            modules.append(".".join(relative.parts))
    return modules


def test_the_detector_finds_the_known_raw_writers():
    """Guards the guard: a detector that finds nothing passes every module vacuously."""
    assert {
        "data_lake.ingestion.news.finnhub_news",
        "data_lake.ingestion.news.newsapi",
    } <= set(_raw_writing_modules())


@pytest.mark.parametrize("module_name", _raw_writing_modules())
def test_a_raw_writing_news_connector_declares_its_person_fields(module_name):
    module = importlib.import_module(module_name)
    fields = getattr(module, "PERSON_FIELDS", None)
    assert fields is not None, (
        f"{module_name} writes `raw` but declares no PERSON_FIELDS (hashed via "
        "ingestion.base.pseudonymize). Keeping a payload whole stores whatever the provider "
        "sends, people included — say which fields are people."
    )
    assert isinstance(fields, tuple), f"{module_name}.PERSON_FIELDS must be a tuple of names"
    assert fields, f"{module_name}.PERSON_FIELDS is empty; a news byline is always a person"
    assert all(isinstance(field, str) and field for field in fields)
