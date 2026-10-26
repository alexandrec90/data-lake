"""How ``pyproject.toml`` spells the packages it declares, which decides whether Dependabot
can update them.

Dependabot's uv updater finds a declaration to rewrite by a case-sensitive match on the
package's normalized, lowercase name. ``"SQLAlchemy>=2.0"`` never matched ``sqlalchemy``,
so every weekly ``uv`` update job from 2026-09-06 on died on
``Handled error whilst updating sqlalchemy: dependency_file_content_not_changed`` -- and
a bump by hand in ``uv.lock`` answered one week, then the next run failed the same way.
"""

import pathlib
import re
import tomllib

from packaging.requirements import Requirement

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def declared() -> list[str]:
    """Every requirement string the project declares: runtime, extras and groups."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    found = list(project["project"].get("dependencies", []))
    for extra in project["project"].get("optional-dependencies", {}).values():
        found += extra
    for group in project.get("dependency-groups", {}).values():
        found += [entry for entry in group if isinstance(entry, str)]
    return found


def test_every_declared_package_is_spelled_as_dependabot_names_it() -> None:
    misspelled = [
        spec for spec in declared() if Requirement(spec).name != Requirement(spec).name.lower()
    ]
    assert misspelled == [], "spell each name in lowercase, or Dependabot cannot rewrite it"


def test_the_declarations_are_read_at_all() -> None:
    """A parse that found nothing would pass the spelling test vacuously."""
    names = {re.sub(r"[-_.]+", "-", Requirement(spec).name).lower() for spec in declared()}
    assert {"sqlalchemy", "pandas", "pytest"} <= names
