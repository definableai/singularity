"""One recursive module loader — used by registrar and middleware discovery.

Strict-loudness: an import error anywhere refuses boot with the full traceback.
"""

import importlib
import pkgutil
from types import ModuleType


def load_package(dotted: str) -> list[ModuleType]:
    """Import every module under a package, recursively. Import error = raise, not skip."""
    pkg = importlib.import_module(dotted)
    modules = [pkg]
    for info in pkgutil.walk_packages(pkg.__path__, prefix=pkg.__name__ + "."):
        modules.append(importlib.import_module(info.name))
    return modules
