"""Shared loader for hook scripts whose filenames contain hyphens.

Hook scripts are not importable as normal modules (hyphenated names, and one
lives outside this tree), so tests load them by path. Each script guards its
side effects behind `if __name__ == '__main__'`, so importing only binds the
pure functions.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Put scripts/ on the path so modules under it (e.g. `diagnostics`) import the
# same way they do at runtime, where the script's own dir is sys.path[0].
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def load_module(relpath: str):
    """Load a hook script (path relative to repo root) as a module object."""
    path = REPO_ROOT / relpath
    mod_name = path.stem.replace("-", "_")
    # Cache like a real import: re-registering an already-loaded name would
    # replace the module object other modules bound at their import time and
    # break cross-module identity (e.g. prune.restart_engine is
    # docker_win.restart_engine).
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        # A missing/unreadable script, or one importlib cannot infer a loader for.
        # Raising here names the path; falling through hands module_from_spec a None
        # and the AttributeError points at importlib internals instead.
        raise ImportError(f"cannot load {relpath} from {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec (importlib docs): stdlib introspection such as
    # dataclasses' string-annotation resolution looks the module up by name.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[mod_name]
        raise
    return module
