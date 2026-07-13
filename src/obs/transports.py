"""Obs transports (05). orjson gives bytes; transports write bytes + newline."""

import sys
from pathlib import Path

# Constants
JSONL_MAX_BYTES = 50 * 1024 * 1024  # size-rotate: events.jsonl → events.jsonl.1


class StdoutTransport:
    """Prod default: one JSON event per line → the platform's log collector."""

    def send(self, batch: list[bytes]) -> None:
        out = sys.stdout.buffer
        for event in batch:
            out.write(event + b"\n")
        out.flush()


class JsonlTransport:
    """Dev default: logs/events.jsonl — greppable, zero deps."""

    def __init__(self, path: str = "logs/events.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, batch: list[bytes]) -> None:
        if self.path.exists() and self.path.stat().st_size > JSONL_MAX_BYTES:
            self.path.rename(self.path.with_suffix(".jsonl.1"))
        with self.path.open("ab") as f:
            for event in batch:
                f.write(event + b"\n")


class NullTransport:
    """Tests."""

    def send(self, batch: list[bytes]) -> None:
        pass


class RedisStreamTransport:
    """Prod add-on: XADD to a capped stream — the durable buffer external consumers read.
    MAXLEN (approximate) is the retention policy. Sync client: we run on the flusher
    thread."""

    STREAM = "obs:events"
    MAXLEN = 100_000

    def __init__(self) -> None:
        import redis as redis_sync

        from src.config.settings import settings

        self._redis = redis_sync.from_url(settings.redis_url)

    def send(self, batch: list[bytes]) -> None:
        pipe = self._redis.pipeline(transaction=False)
        for event in batch:
            pipe.xadd(self.STREAM, {"e": event}, maxlen=self.MAXLEN, approximate=True)
        pipe.execute()


REGISTRY = {
    "stdout": StdoutTransport,
    "jsonl": JsonlTransport,
    "null": NullTransport,
    "redis_stream": RedisStreamTransport,
}
