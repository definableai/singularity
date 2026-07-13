"""DB integration tests — require the compose Postgres; skipped when unreachable."""

import socket
from urllib.parse import urlparse

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.config.settings import settings


def _pg_reachable() -> bool:
    if not settings.database_url:
        return False
    host = urlparse(settings.database_url.replace("+asyncpg", "")).hostname or "localhost"
    port = urlparse(settings.database_url.replace("+asyncpg", "")).port or 5432
    try:
        socket.create_connection((host, port), timeout=1).close()
        return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")


@pytest.fixture(scope="module")
def db_app():
    """App with one service that writes, one that fails after writing."""
    from src.core import registrar
    from src.core.context import Context
    from src.core.errors import register_handlers
    from src.database.engine import get_db, get_engine

    class ItemService:
        http_exposed = ["post=create", "post=fail", "get=count"]

        def __init__(self, ctx):
            self.ctx = ctx

        async def post_create(self, session=Depends(get_db)) -> dict:
            await session.execute(text("INSERT INTO _t_items (name) VALUES ('a')"))
            return {"ok": True}

        async def post_fail(self, session=Depends(get_db)) -> dict:
            await session.execute(text("INSERT INTO _t_items (name) VALUES ('doomed')"))
            raise ValueError("after write")

        async def get_count(self, session=Depends(get_db)) -> dict:
            n = (await session.execute(text("SELECT count(*) FROM _t_items"))).scalar()
            return {"count": n}

    class _Mod:
        __name__ = "src.services.items.service"

    m = _Mod()
    ItemService.__module__ = m.__name__
    m.ItemService = ItemService

    import asyncio

    async def setup():
        engine = get_engine()
        await engine.dispose()  # shed connections earlier tests' lifespans pooled on their loops
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS _t_items"))
            await conn.execute(text("CREATE TABLE _t_items (id serial primary key, name text)"))
        # pooled asyncpg connections bind to THIS loop; dispose so TestClient's loop
        # gets fresh ones
        await engine.dispose()

    asyncio.run(setup())

    app = FastAPI()
    import unittest.mock as mock

    with mock.patch.object(registrar, "load_package", lambda pkg: [m]):
        registrar.register_services(app, Context(settings))
    register_handlers(app, dev=True)
    return app


@pytest.fixture(scope="module")
def db_client(db_app):
    # ONE TestClient (= one event loop) for the whole module: pooled asyncpg
    # connections are loop-bound, a client per test would poison the pool
    with TestClient(db_app, raise_server_exceptions=False) as c:
        yield c
    import asyncio

    from src.database.engine import get_engine

    async def teardown():
        engine = get_engine()
        await engine.dispose()
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS _t_items"))  # keep alembic check clean
        await engine.dispose()

    asyncio.run(teardown())


def test_commit_before_response(db_client):
    assert db_client.post("/api/v1/items/create").json() == {"ok": True}
    assert db_client.get("/api/v1/items/count").json()["count"] == 1


def test_rollback_on_unhandled_error(db_client):
    before = db_client.get("/api/v1/items/count").json()["count"]
    r = db_client.post("/api/v1/items/fail")
    assert r.status_code == 500
    # the write before the exception must be rolled back, not committed
    assert db_client.get("/api/v1/items/count").json()["count"] == before


def test_sql_steps_recorded():
    # engine event hooks feed sql steps into the current journey — synthetic run on a
    # fresh loop; dispose around it because pooled connections are loop-bound
    import asyncio

    from src.database.engine import get_engine, session_factory
    from src.tracing import journey as jmod

    async def run():
        await get_engine().dispose()  # shed connections bound to other tests' loops
        j = jmod.start("GET", "/synthetic", "req_x")
        async with session_factory()() as s:
            await s.execute(text("SELECT 1"))
        await get_engine().dispose()
        return j

    j = asyncio.run(run())
    sql_steps = [s for s in j.steps if s["kind"] == "sql"]
    assert len(sql_steps) == 1
    assert sql_steps[0]["name"].startswith("SELECT 1")
    assert sql_steps[0]["duration_ms"] > 0
