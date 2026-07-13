"""obs.init() — one call wires the whole pipeline; API and worker both use it (05)."""

import logging

from src.config.settings import Settings
from src.obs.pipeline import Pipeline
from src.obs.transports import REGISTRY

# Constants
REDACT_FIELDS = {"password", "token", "authorization", "cookie", "api_key", "secret"}

_pipeline: Pipeline | None = None


def _principal_id() -> str:
    from src.auth.deps import principal_id_var

    return principal_id_var.get("")


def get_pipeline() -> Pipeline | None:
    return _pipeline


def init(settings: Settings) -> Pipeline:
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    transports = [REGISTRY[name]() for name in settings.obs_transports]
    _pipeline = Pipeline(
        transports,
        REDACT_FIELDS | set(settings.obs_redact_fields),
        env={"app": "singularity", "environment": settings.environment},
    )
    if settings.database_url:
        from src.obs.store import PGStore

        _pipeline.store = PGStore(settings.database_url.replace("+asyncpg", ""))
    _pipeline.start()

    _register_capture_sink(_pipeline, settings)
    _bridge_stdlib(settings)
    return _pipeline


def _register_capture_sink(pipeline: Pipeline, settings: Settings) -> None:
    from loguru import logger

    capture_level = "DEBUG" if settings.is_dev else "INFO"

    from src.tracing.journey import trace_id_var

    def sink(message) -> None:
        r = message.record
        if "/__obs" in r["message"]:
            return  # self-exclusion (09): dashboard access logs don't feed the store
        # Hot path: shallow copies + one deque append; envelope work is the flusher's.
        pipeline.enqueue(
            "log",
            {
                "level": r["level"].name,
                "message": r["message"],
                "trace_id": trace_id_var.get(""),
                "principal_id": _principal_id(),
                "logger": {
                    "module": r["name"],
                    "line": r["line"],
                    "function": r["function"],
                },
                "extra": {k: v for k, v in r["extra"].items() if k != "request_id"},
            },
        )

    logger.add(sink, level=capture_level, backtrace=False, diagnose=False)

    # The two non-log verbs — one author-facing surface, four event kinds (05).
    def metric(name: str, value: float = 1, **kv) -> None:
        pipeline.enqueue("metric", {"message": name, "extra": {"value": value, **kv}})

    def audit(action: str, **kv) -> None:
        # Bypasses the level filter by design; same queue semantics as everything else.
        pipeline.enqueue("audit", {"message": action, "extra": kv})

    logger.metric = metric
    logger.audit = audit


class _InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        from loguru import logger

        logger.opt(depth=6, exception=record.exc_info).log(
            record.levelname if record.levelname in logging._nameToLevel else "INFO",
            record.getMessage(),
        )


def _bridge_stdlib(settings: Settings) -> None:
    # Third-party logs (uvicorn, sqlalchemy, celery) join the same pipeline.
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
    # Libraries that attach their own handlers with propagate=False (uvicorn) would
    # bypass root — strip them so everything funnels through the intercept.
    for name in list(logging.root.manager.loggerDict):
        existing = logging.getLogger(name)
        existing.handlers = []
        existing.propagate = True
