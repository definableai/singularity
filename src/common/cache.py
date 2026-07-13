"""ctx.cache (01): namespaced Redis cache. orjson only (pickle is an attack surface),
TTL mandatory, fail-open behind a ~5s breaker, @cached with single-flight.
"""

import asyncio
import time
from typing import Any

import orjson

# Constants
BREAKER_COOLDOWN_S = 5.0
SINGLE_FLIGHT_WAIT_S = 10.0


class Cache:
    def __init__(self, app: str, env: str):
        self.prefix = f"{app}:{env}"
        self._breaker_until = 0.0
        self._inflight: dict[str, asyncio.Future] = {}

    def _k(self, namespace: str, key: str) -> str:
        return f"{self.prefix}:{namespace}:{key}"

    def _open(self) -> bool:
        return time.monotonic() >= self._breaker_until

    def _trip(self) -> None:
        # A cache outage must not become a per-request connect-timeout latency storm.
        self._breaker_until = time.monotonic() + BREAKER_COOLDOWN_S

    async def get(self, namespace: str, key: str) -> Any | None:
        if not self._open():
            return None
        try:
            from src.common.redis import get_redis

            raw = await get_redis().get(self._k(namespace, key))
            return orjson.loads(raw) if raw is not None else None
        except Exception:
            self._trip()
            return None  # read error = miss

    async def set(self, namespace: str, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            raise ValueError("ttl is mandatory — no immortal keys")
        if not self._open():
            return
        try:
            from src.common.redis import get_redis

            await get_redis().set(self._k(namespace, key), orjson.dumps(value), ex=ttl)
        except Exception:
            self._trip()  # write error = skip

    async def invalidate(self, namespace: str, key: str) -> None:
        try:
            from src.common.redis import get_redis

            await get_redis().delete(self._k(namespace, key))
        except Exception:
            self._trip()

    def cached(self, ttl: int, namespace: str = "fn"):
        """@ctx.cache.cached(ttl=60) — single-flight: concurrent misses on one key →
        one computes, the rest await. The computation runs detached so a disconnecting
        caller can't cancel it for everyone; exceptions resolve the shared future."""

        def decorator(fn):
            async def wrapper(*args, **kwargs):
                key = f"{fn.__module__}.{fn.__qualname__}:{args!r}:{sorted(kwargs.items())!r}"
                hit = await self.get(namespace, key)
                if hit is not None:
                    return hit
                if key in self._inflight:
                    try:
                        return await asyncio.wait_for(
                            asyncio.shield(self._inflight[key]), SINGLE_FLIGHT_WAIT_S
                        )
                    except (TimeoutError, Exception):
                        pass  # fall through: compute ourselves
                fut = asyncio.get_event_loop().create_future()
                self._inflight[key] = fut
                try:
                    result = await asyncio.shield(asyncio.create_task(fn(*args, **kwargs)))
                    fut.set_result(result)
                    await self.set(namespace, key, result, ttl)
                    return result
                except BaseException as e:
                    if not fut.done():
                        fut.set_exception(e)
                        fut.exception()  # mark retrieved: waiters get it, loop stays quiet
                    raise
                finally:
                    self._inflight.pop(key, None)

            return wrapper

        return decorator
