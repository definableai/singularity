"""Obs pipeline (05): bounded ring → flusher thread → transports.

Call-site budget ≤5µs: capture appends a tuple to a deque, nothing else. Envelope
assembly, redaction, and orjson encoding all happen on the flusher thread.
"""

import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import orjson

from src.core.asgi import request_id_var

# Constants (01's earning rule)
QUEUE_SIZE = 10_000
FLUSH_INTERVAL_S = 0.5
BATCH_SIZE = 500
STDERR_REPORT_INTERVAL_S = 30.0

_HOST = socket.gethostname()


class Pipeline:
    def __init__(self, transports: list, redact_fields: set[str], env: dict[str, Any]):
        self.transports = transports
        self.redact_fields = redact_fields
        self.env = env
        self.store = None  # PGStore (09), attached by obs.init when DATABASE_URL is set
        self.queue: deque = deque(maxlen=QUEUE_SIZE)
        self.dropped = 0
        self.encode_errors = 0
        self.transport_errors = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_report = time.monotonic()
        self._last_rollup = time.monotonic()

    # -- call site (hot path) ------------------------------------------------

    def enqueue(self, kind: str, payload: dict[str, Any]) -> None:
        if len(self.queue) >= QUEUE_SIZE:
            self.dropped += 1  # drop-oldest happens implicitly via maxlen
        self.queue.append((kind, time.time(), request_id_var.get(""), payload))

    # -- flusher thread ------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="obs-flusher", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(FLUSH_INTERVAL_S)
            try:  # the flusher cannot die from a batch failure (05)
                self._emit_rollups()
                self._drain()
                self._report_loss()
            except Exception as e:
                print(f"obs-flusher iteration error: {e!r}", file=sys.stderr)

    def _emit_rollups(self) -> None:
        # Pre-aggregated RED (05): one rollup per active route per window — never one
        # metric event per request (86M events/day at 1k RPS for aggregate data).
        from src.obs import red

        if time.monotonic() - self._last_rollup < red.WINDOW_S:
            return
        self._last_rollup = time.monotonic()
        for payload in red.drain():
            self.enqueue("metric", payload)

    def _drain(self) -> None:
        while self.queue:
            batch = []
            for _ in range(min(BATCH_SIZE, len(self.queue))):
                try:
                    batch.append(self.queue.popleft())
                except IndexError:
                    break
            if not batch:
                return
            envelopes, encoded = [], []
            for item in batch:
                try:
                    envelope = self._envelope(*item)
                    envelopes.append(envelope)
                    encoded.append(orjson.dumps(envelope))
                except Exception:
                    self.encode_errors += 1
            if self.store is not None and envelopes:
                self.store.write(envelopes)  # raises nothing; counts its own errors
            for t in self.transports:
                try:
                    t.send(encoded)
                except Exception as e:
                    self.transport_errors += 1
                    print(f"obs transport {type(t).__name__} failed: {e!r}", file=sys.stderr)

    def _envelope(self, kind: str, ts: float, request_id: str, payload: dict) -> dict:
        ctx = {"request_id": request_id}
        for key in ("trace_id", "task", "principal_id"):
            if key in payload:
                ctx[key] = payload.pop(key)
        extra = payload.pop("extra", None)
        return {
            "v": 1,
            "kind": kind,
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            **payload,
            "ctx": ctx,
            "env": self.env | {"host": _HOST},
            "extra": self._redact(extra) if extra else {},
        }

    def _redact(self, obj: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "…"
        if isinstance(obj, dict):
            return {
                k: "[redacted]" if k.lower() in self.redact_fields else self._redact(v, depth + 1)
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [self._redact(v, depth + 1) for v in obj]
        return obj

    def _report_loss(self) -> None:
        # Loss reports go out-of-band: reporting pipeline loss through the pipeline is circular.
        now = time.monotonic()
        if now - self._last_report < STDERR_REPORT_INTERVAL_S:
            return
        self._last_report = now
        if self.dropped or self.encode_errors or self.transport_errors:
            print(
                f"obs pipeline loss: dropped={self.dropped} encode_errors={self.encode_errors} "
                f"transport_errors={self.transport_errors}",
                file=sys.stderr,
            )

    def shutdown(self, timeout_s: float) -> None:
        """Bounded final flush — a down transport at shutdown must not hang until SIGKILL."""
        deadline = time.monotonic() + timeout_s
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(0.1, deadline - time.monotonic()))
        if time.monotonic() < deadline:
            try:
                self._drain()
            except Exception as e:
                print(f"obs shutdown flush error: {e!r}", file=sys.stderr)
