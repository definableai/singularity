"""ctx.cache + ctx.lock tests — need compose Redis/PG; skipped when unreachable."""

import asyncio

import pytest

from tests.test_db import _pg_reachable


def _redis_reachable() -> bool:
    import socket
    from urllib.parse import urlparse

    from src.config.settings import settings

    u = urlparse(settings.redis_url)
    try:
        socket.create_connection((u.hostname or "localhost", u.port or 6379), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _redis_reachable(), reason="redis not reachable")
def test_cache_roundtrip_and_single_flight():
    import uuid

    from src.common.cache import Cache

    cache = Cache("test", "dev")
    calls = 0
    nonce = uuid.uuid4().hex  # cache persists in redis across pytest runs — unique key

    @cache.cached(ttl=5)
    async def compute(x: str) -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"x": x}

    results = asyncio.run(_gather5(compute, nonce))
    assert all(r == {"x": nonce} for r in results)
    assert calls == 1  # single-flight: five concurrent misses, one computation


async def _gather5(fn, arg):
    return await asyncio.gather(*[fn(arg) for _ in range(5)])


@pytest.mark.skipif(not _redis_reachable(), reason="redis not reachable")
def test_cache_ttl_mandatory():
    from src.common.cache import Cache

    with pytest.raises(ValueError):
        asyncio.run(Cache("test", "dev").set("ns", "k", 1, ttl=0))


@pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")
def test_lock_mutual_exclusion():
    from src.common.lock import LockManager

    order: list[str] = []

    async def flow():
        from src.database.engine import get_engine

        await get_engine().dispose()
        lock = LockManager()

        async def worker(tag: str):
            async with lock("test:mutex"):
                order.append(f"{tag}-in")
                await asyncio.sleep(0.05)
                order.append(f"{tag}-out")

        await asyncio.gather(worker("a"), worker("b"))
        await get_engine().dispose()

    asyncio.run(flow())
    # critical sections never interleave: every -in is followed by its own -out
    assert order[0].split("-")[0] == order[1].split("-")[0]
    assert order[2].split("-")[0] == order[3].split("-")[0]
