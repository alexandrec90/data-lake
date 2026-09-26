"""What a consumer's type checker sees when it imports this package.

Without a PEP 561 ``py.typed`` marker, mypy treats ``data_lake`` as an untyped third-party
module in every consumer: each import resolves to ``Any`` (or an ``import-untyped`` error),
so the annotations this package carries buy its consumers nothing. The marker has to sit
beside the imported package itself -- consumers install it editable from a sibling checkout,
so that is ``src/data_lake/``, and hatchling ships it into the wheel from the same place.
"""

import pathlib

import data_lake


def test_package_ships_a_py_typed_marker() -> None:
    package_dir = pathlib.Path(data_lake.__file__).resolve().parent
    assert (package_dir / "py.typed").is_file()
