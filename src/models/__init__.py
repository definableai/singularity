"""Model autodiscovery (02): every module here is imported, so alembic autogenerate can
never silently miss a model. One file per domain: src/models/<domain>_model.py."""

import importlib
import pkgutil

for _info in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_info.name}")
