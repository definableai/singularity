"""ctx.http (01): one shared outbound client — timeouts ON by default (an untimed
outbound call hangs a worker), request-id + trace headers injected, every call a
journey `http` step. App code never constructs ad-hoc clients."""

import time

import httpx

# Constants
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 30.0

_client: httpx.AsyncClient | None = None


class _Tracing(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from src.core.asgi import request_id_var
        from src.tracing import journey
        from src.tracing.journey import trace_id_var

        if rid := request_id_var.get(""):
            request.headers.setdefault("X-Request-ID", rid)
        if tid := trace_id_var.get(""):
            request.headers.setdefault("X-Trace-ID", tid)
        t0 = time.perf_counter()
        try:
            response = await self.inner.handle_async_request(request)
            return response
        finally:
            if (j := journey.current()) is not None:
                status = locals().get("response")
                j.add_step(
                    "http",
                    f"{request.method} {request.url.host}{request.url.path}"[:120],
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    status=status.status_code if status else "error",
                )


def get_http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(TOTAL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S),
            transport=_Tracing(httpx.AsyncHTTPTransport(retries=1)),
        )
    return _client
