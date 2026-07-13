"""Sweep tests: ctx.http, ws manager, as_user fixture, redis_stream, backpressure."""

import asyncio

import pytest


def test_ctx_http_injects_trace_headers_and_records_step(client):
    import httpx

    from src.common import http as http_mod
    from src.core.asgi import request_id_var
    from src.tracing import journey as jmod

    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    async def flow():
        request_id_var.set("req_test")
        j = jmod.start("GET", "/outbound-test", "req_test")
        inner = http_mod._Tracing(httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=inner) as c:
            r = await c.get("https://api.example.com/v1/thing")
        return j, r

    j, r = asyncio.run(flow())
    assert r.status_code == 200
    assert captured["x-request-id"] == "req_test"
    assert captured["x-trace-id"] == j.trace_id
    step = next(s for s in j.steps if s["kind"] == "http")
    assert step["name"] == "GET api.example.com/v1/thing"
    assert step["data"]["status"] == 200


def test_ws_manager_broadcast_and_dead_cleanup():
    from src.common.websocket import WebSocketManager

    class FakeWs:
        def __init__(self, alive=True):
            self.alive = alive
            self.got = []

        async def send_json(self, m):
            if not self.alive:
                raise RuntimeError("gone")
            self.got.append(m)

    m = WebSocketManager()
    good, dead = FakeWs(), FakeWs(alive=False)
    m._connections["room"] = {good, dead}
    n = asyncio.run(m.broadcast("room", {"x": 1}))
    assert good.got == [{"x": 1}]
    assert dead not in m._connections["room"]
    assert n == 1  # dead one evicted


def test_client_as_user_fixture(client):
    from fastapi import Depends

    from src.app import app
    from src.auth.deps import Auth
    from src.auth.protocol import Principal

    @app.get("/api/v1/_whoami", include_in_schema=False)
    async def whoami(user: Principal = Depends(Auth())) -> dict:
        return {"id": user.id, "kind": user.kind, "claims": user.claims}

    r = client.as_user("usr_9", claims={"role": "admin"}).get("/api/v1/_whoami")
    assert r.json() == {"id": "usr_9", "kind": "user", "claims": {"role": "admin"}}


def test_redis_stream_transport():
    import socket
    from urllib.parse import urlparse

    from src.config.settings import settings

    u = urlparse(settings.redis_url)
    try:
        socket.create_connection((u.hostname, u.port or 6379), timeout=1).close()
    except OSError:
        pytest.skip("redis not reachable")

    import redis as redis_sync

    from src.obs.transports import RedisStreamTransport

    t = RedisStreamTransport()
    r = redis_sync.from_url(settings.redis_url)
    before = r.xlen(t.STREAM)
    t.send([b'{"kind":"log","message":"stream-test"}'])
    assert r.xlen(t.STREAM) == before + 1


def test_backpressure_sheds_tiers(monkeypatch):
    from src.obs import get_pipeline
    from src.obs.pipeline import QUEUE_SIZE
    from src.tracing import engine

    p = get_pipeline()
    if p is None:
        import src.obs as obs
        from src.config.settings import settings

        p = obs.init(settings)
    monkeypatch.setattr(p, "queue", type(p.queue)([("x",)] * int(QUEUE_SIZE * 0.9), maxlen=QUEUE_SIZE))
    assert engine._backpressure_tier(2) == 0  # over high watermark → T0 only
    monkeypatch.setattr(p, "queue", type(p.queue)([("x",)] * int(QUEUE_SIZE * 0.5), maxlen=QUEUE_SIZE))
    assert engine._backpressure_tier(2) == 1  # half full → shed lines, keep calls
    monkeypatch.setattr(p, "queue", type(p.queue)([], maxlen=QUEUE_SIZE))
    assert engine._backpressure_tier(2) == 2  # healthy → untouched


def test_pagination_envelope():
    from src.common.pagination import Page, page_params, paginated

    p = page_params(limit=10, offset=20)
    assert p == Page(limit=10, offset=20)
    assert paginated(["a"], 41, p) == {"items": ["a"], "total": 41, "limit": 10, "offset": 20}


def test_sync_session_for_tasks():
    from tests.test_db import _pg_reachable

    if not _pg_reachable():
        import pytest

        pytest.skip("postgres not reachable")
    from sqlalchemy import text

    from src.database.engine import get_sync_session

    with get_sync_session() as session:
        assert session.execute(text("SELECT 41 + 1")).scalar() == 42
