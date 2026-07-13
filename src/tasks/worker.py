"""Worker entrypoint (04): `uv run worker` → celery worker, threads pool, no beat.

Threads give gevent-level IO concurrency with zero monkey-patching. Beat is its own
process (`uv run worker --beat`), single replica — no duplicate periodic tasks.
User task modules in src/tasks/*.py are autodiscovered (strict: bad import = error).
"""

import importlib
import pkgutil
import sys

# Constants
CONCURRENCY = 16


def _discover_task_modules() -> None:
    import src.tasks as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name not in ("celery_app", "worker") and not info.name.startswith("_"):
            importlib.import_module(f"src.tasks.{info.name}")


def main() -> None:
    import src.obs as obs
    from src.config.settings import settings
    from src.tasks.celery_app import celery_app

    obs.init(settings)
    _discover_task_modules()

    if "--beat" in sys.argv:
        celery_app.Beat(loglevel="info").run()
    else:
        celery_app.Worker(
            pool="threads",
            concurrency=CONCURRENCY,
            loglevel="info",
            without_beat=True,
        ).start()


if __name__ == "__main__":
    main()
