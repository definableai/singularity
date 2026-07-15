"""The core ASGI layer — one fused pure-ASGI wrapper (01).

Does, per request: request_id → (RED counters, 05) → (T0 journey bracket, 06) →
body limit → request timeout. RED/T0 hooks land with plans 05/06; the seams are marked.
"""

import asyncio
import time
import traceback
import uuid
from contextvars import ContextVar

import orjson

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Constants (01's earning rule — edit source to tune)
MAX_BODY_BYTES = 10 * 1024 * 1024


def _envelope_bytes(code: str, message: str) -> bytes:
    return orjson.dumps(
        {"error": {"code": code, "message": message, "request_id": request_id_var.get("")}}
    )


class CoreLayer:
    def __init__(
        self,
        app,
        timeout_s: int,
        trace_mode: str = "off",
        trace_slow_ms: int = 1000,
        trace_sample_rate: float = 0.1,
    ):
        self.app = app
        self.timeout_s = timeout_s
        self.trace_mode = trace_mode
        self.trace_slow_ms = trace_slow_ms
        self.trace_sample_rate = trace_sample_rate

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        request_id = "req_" + uuid.uuid4().hex[:20]
        request_id_var.set(request_id)

        if scope["type"] == "websocket":
            return await self.app(scope, receive, send)

        if scope["path"].startswith("/__obs"):
            # self-exclusion (09): a dashboard observing itself fills its own store
            return await self.app(scope, receive, send)

        from src.tracing import engine, journey

        j = journey.start(scope["method"], scope["path"], request_id)
        j.query_string = scope.get("query_string", b"").decode(errors="replace")
        if self.trace_mode == "dev":
            engine.arm(j, tier=2)
        elif self.trace_mode == "on_error":
            import random

            if random.random() < self.trace_sample_rate:
                engine.arm(j, tier=2)  # captured always, emitted only if interesting
        start_time = time.perf_counter()
        response_started = False
        status_code = 0
        body_seen = 0

        async def limited_receive():
            nonlocal body_seen
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                body_seen += len(chunk)
                if body_seen > MAX_BODY_BYTES:
                    raise _BodyTooLarge()
                # replay input (06): capture armed non-GET bodies, capped — redaction
                # happens with the rest of the envelope on the flusher (05)
                if j.tier >= 1 and scope["method"] not in ("GET", "HEAD") and chunk:
                    if len(j.body) < journey.BODY_CAP_BYTES:
                        j.body += chunk[: journey.BODY_CAP_BYTES - len(j.body)].decode(
                            errors="replace"
                        )
            return message

        async def tracked_send(message):
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = message.setdefault("headers", [])
                headers.append((b"x-request-id", request_id.encode()))
            await send(message)

        async def _reject(status: int, code: str, message: str):
            body = _envelope_bytes(code, message)
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"x-request-id", request_id.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        try:
            async with asyncio.timeout(self.timeout_s):
                await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            status_code = 413
            if not response_started:
                await _reject(413, "body_too_large", f"request body over {MAX_BODY_BYTES} bytes")
        except TimeoutError:
            status_code, j.error = 504, f"request exceeded {self.timeout_s}s"
            j.add_step("exception", "request_timeout")
            if not response_started:
                await _reject(504, "request_timeout", f"request exceeded {self.timeout_s}s")
            else:
                raise
        except Exception as exc:
            # Unhandled exceptions pass through here BEFORE the outermost error
            # middleware builds the 500 — this is the one place they get recorded.
            status_code = 500
            j.error = f"{type(exc).__name__}: {exc}"
            j.add_step(
                "exception",
                type(exc).__name__,
                message=str(exc)[:500],
                handled=False,
                trace=traceback.format_exc(limit=20)[:4000],
            )
            raise
        finally:
            engine.disarm(j)
            duration_ms = (time.perf_counter() - start_time) * 1000
            route = scope.get("route")
            route_path = getattr(route, "path", None) or scope["path"]
            from src.obs import red

            red.observe(
                "request",
                route_path,
                scope["method"],
                f"{status_code // 100}xx" if status_code else "0xx",
                duration_ms,
            )
            payload = journey.finish(j, status_code)
            if journey.should_emit(j, duration_ms, self.trace_mode, self.trace_slow_ms):
                from src.obs import get_pipeline

                if (p := get_pipeline()) is not None:
                    p.enqueue("journey", payload)


class _BodyTooLarge(Exception):
    pass
