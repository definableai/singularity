"""api_key provider (03). Unsalted sha256 is sound ONLY for generated high-entropy keys —
so the generator ships and user-chosen key material has no entry point. Lookup is an
in-process TTL cache with bounded negative caching: invalid-key floods must hammer
neither PG nor the cache's memory. Revocation caveat: a revoked key stays valid up to
TTL per process — the short default is the price of the hot path.
"""

import hashlib
import secrets
import time

from fastapi import Request, WebSocket
from sqlalchemy import select

from src.auth.protocol import AuthError, Principal
from src.config.settings import settings

# Constants
NEGATIVE_CACHE_MAX = 10_000


def generate_key() -> tuple[str, str, str]:
    """→ (plaintext shown once, sha256 hash to store, prefix for UIs)."""
    plaintext = "sk_" + secrets.token_urlsafe(32)
    return plaintext, hashlib.sha256(plaintext.encode()).hexdigest(), plaintext[:8]


class ApiKeyProvider:
    name = "api_key"

    def __init__(self) -> None:
        self.ttl = settings.api_key_cache_ttl
        self._hits: dict[str, tuple[float, Principal]] = {}
        self._misses: dict[str, float] = {}

    async def authenticate(self, request: Request | WebSocket) -> Principal | None:
        raw = request.headers.get("x-api-key")
        if not raw:
            return None
        digest = hashlib.sha256(raw.encode()).hexdigest()
        now = time.monotonic()

        if (hit := self._hits.get(digest)) and now - hit[0] < self.ttl:
            return hit[1]
        if now - self._misses.get(digest, -1e9) < self.ttl:
            raise AuthError("invalid api key")

        principal = await self._lookup(digest)
        if principal is None:
            if len(self._misses) >= NEGATIVE_CACHE_MAX:
                self._misses.clear()  # bounded: reset beats unbounded growth
            self._misses[digest] = now
            raise AuthError("invalid api key")
        self._hits[digest] = (now, principal)
        return principal

    async def _lookup(self, digest: str) -> Principal | None:
        from src.database.engine import session_factory
        from src.models.api_key_model import ApiKey

        async with session_factory()() as session:
            row = (
                await session.execute(
                    select(ApiKey).where(ApiKey.key_hash == digest, ApiKey.revoked.is_(False))
                )
            ).scalar_one_or_none()
        if row is None:
            return None
        return Principal(id=row.principal_id, kind="api_key", claims={"key_name": row.name})
