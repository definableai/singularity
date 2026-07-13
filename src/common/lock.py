"""ctx.lock (01): distributed lock as an async context manager — PG advisory only.

08's script runner reuses this. The lock lives on a dedicated connection held for the
lock's lifetime; connection death = lock loss, which PG handles by releasing.
"""

import hashlib
from contextlib import asynccontextmanager


def _key(name: str) -> int:
    # stable 63-bit key from the name
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big") >> 1


class LockManager:
    @asynccontextmanager
    async def __call__(self, name: str):
        from sqlalchemy import text

        from src.database.engine import get_engine

        key = _key(name)
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            try:
                yield
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
