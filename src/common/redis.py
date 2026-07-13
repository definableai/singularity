"""Raw async Redis client. ctx.cache semantics (single-flight etc.) land in build step 5."""

import redis.asyncio as aioredis

from src.config.settings import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _client
