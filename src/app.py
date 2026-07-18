"""create_app() + dev/prod runner (01)."""

import sys
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from src.common.logger import log
from src.config.settings import settings
from src.core.asgi import CoreLayer
from src.core.context import Context
from src.core.errors import register_handlers
from src.core.middleware import discover_middlewares
from src.core.registrar import register_services


def create_app() -> FastAPI:
    ctx = Context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import src.obs as obs

        pipeline = obs.init(settings)
        killer = None
        if settings.trace_mode != "off":
            import asyncio

            from src.tracing import engine

            if not engine.init(settings.trace_code_roots):
                log.warning("tracer tool id unavailable (coverage/profiler running?) — T0 only")
            killer = asyncio.create_task(engine.kill_switch_refresher())
        await _fail_fast_checks()
        if settings.database_url:
            from src.core.schema import ensure_schema

            await ensure_schema()
            if settings.is_dev:
                # dev: pending scripts run at startup; prod: explicit deploy step (08)
                from src.core.scripts import run_pending

                ran = await run_pending(ctx, triggered_by="startup")
                if ran:
                    log.info(f"scripts run at startup: {ran}")
        log.info(
            f"boot report — environment={settings.environment} "
            f"obs_transports={settings.obs_transports} trace_mode={settings.trace_mode} "
            f"db={'configured' if settings.database_url else 'DISABLED (no DATABASE_URL)'}"
        )
        for line in app.state.route_report:
            log.info(f"  {line}")
        yield
        if killer is not None:
            killer.cancel()
        pipeline.shutdown(settings.obs_flush_timeout_s)

    # No default_response_class: modern FastAPI serializes annotated returns straight to
    # JSON bytes via Pydantic — faster than ORJSONResponse. orjson still encodes error
    # envelopes (core/errors.py) and the core layer's rejects (core/asgi.py).
    app = FastAPI(
        lifespan=lifespan,
        docs_url="/docs" if settings.is_dev else None,
        openapi_url="/openapi.json" if settings.is_dev else None,
    )
    app.state.ctx = ctx

    @app.get("/livez", include_in_schema=False)
    async def livez():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        # LB routing signal: dependency blip pulls the pod from rotation. Own short
        # timeout — must not hang in the pool-checkout queue (02).
        import asyncio

        from src.core.responses import JSONResponse

        checks: dict[str, str] = {}
        if settings.database_url:
            try:
                from sqlalchemy import text

                from src.database.engine import get_engine

                async with asyncio.timeout(1):
                    async with get_engine().connect() as conn:
                        await conn.execute(text("SELECT 1"))
                checks["db"] = "ok"
            except Exception as e:
                checks["db"] = f"fail: {type(e).__name__}"
        try:
            from src.common.redis import get_redis

            async with asyncio.timeout(1):
                await get_redis().ping()
            checks["redis"] = "ok"
        except Exception as e:
            checks["redis"] = f"fail: {type(e).__name__}"
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ready, **checks}, status_code=200 if ready else 503)

    app.state.route_report = register_services(app, ctx)
    register_handlers(app, dev=settings.is_dev)

    # Observatory (09): dev → open; staging/prod → only with DASHBOARD_TOKEN set
    if settings.database_url and (settings.is_dev or settings.dashboard_token):
        from src.obs.dashboard import router as obs_router

        app.include_router(obs_router)
    elif not settings.is_dev:
        log.warning("Observatory disabled: set DASHBOARD_TOKEN to enable /__obs")

    # add_middleware: last added = outermost. User escape-hatch runs inside CoreLayer.
    # cast: the contract is structural (pure ASGI class, core/middleware.py), and
    # Starlette's matching factory protocol is private — nothing public to declare.
    for mw in reversed(discover_middlewares()):
        app.add_middleware(cast(Any, mw))
    app.add_middleware(
        CoreLayer,
        timeout_s=settings.request_timeout_s,
        trace_mode=settings.trace_mode,
        trace_slow_ms=settings.trace_slow_ms,
        trace_sample_rate=settings.trace_sample_rate,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    return app


async def _fail_fast_checks() -> None:
    """No half-alive containers: unreachable configured dependency → refuse to start.
    Dev is lenient about Redis (warn) so a fresh clone boots before compose is up."""
    import asyncio

    if settings.database_url:
        from sqlalchemy import text

        from src.database.engine import get_engine

        deadline = 10
        for attempt in range(deadline * 2):
            try:
                async with asyncio.timeout(2):
                    async with get_engine().connect() as conn:
                        await conn.execute(text("SELECT 1"))
                break
            except Exception as e:
                if attempt == deadline * 2 - 1:
                    log.error(f"Postgres unreachable after {deadline}s: {e!r} — refusing to start")
                    raise SystemExit(1) from None
                await asyncio.sleep(0.5)
    try:
        from src.common.redis import get_redis

        async with asyncio.timeout(2):
            await get_redis().ping()
    except Exception as e:
        if settings.is_dev:
            log.warning(f"Redis unreachable ({e!r}) — cache/tasks degraded until it's up")
        else:
            log.error(f"Redis unreachable: {e!r} — refusing to start")
            raise SystemExit(1) from None


app = create_app()


def main() -> None:
    import uvicorn

    dev = "--dev" in sys.argv or settings.is_dev
    uvicorn.run(
        "src.app:app",
        host="127.0.0.1" if dev else "0.0.0.0",
        port=8000,
        reload=dev,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        timeout_graceful_shutdown=settings.shutdown_grace_s,
    )


if __name__ == "__main__":
    main()
