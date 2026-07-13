# 01 — Core

Status: building — shipped: settings, registrar, Context (settings/cache/lock live),
core ASGI layer, errors, /livez + /readyz, fail-fast pings, ctx.cache (single-flight,
fail-open breaker), ctx.lock (PG advisory). ctx.http shipped (shared client, timeouts on, request-id/trace headers, http journey
step), ws manager shipped (single-replica contract + prod warning, dead-conn eviction).
Pagination envelope shipped (page_params/paginated). Complete for v1 (drain =
uvicorn timeout_graceful_shutdown + bounded obs flush).

App factory, settings, service auto-registration, Context, the core ASGI layer, errors,
websockets.

## create_app()

- `src/app.py`: `app = create_app()`. Lifespan handles startup hooks (obs flusher start,
  provider warmups declared by the project) and graceful shutdown.
- **Fail-fast startup checks**: lifespan pings Postgres and Redis with a short retry
  window before serving; unreachable dependency → refuse to start, loud error. No
  half-alive containers.
- **Boot report**: after successful startup, log one table — routes registered (per
  service), auth provider chain, obs transports, trace mode + store status (09), the active
  `ENVIRONMENT` and every dev affordance currently enabled, pending migrations/scripts
  count. Strict validation says what's wrong; the report says what *is*.
- **Graceful drain**: SIGTERM → stop accepting new connections → drain in-flight
  requests up to `SHUTDOWN_GRACE_S` (default 20) → final obs flush bounded by
  `OBS_FLUSH_TIMEOUT_S` (05) → exit. README documents the k8s pairing:
  `terminationGracePeriodSeconds ≥ preStop + SHUTDOWN_GRACE_S + OBS_FLUSH_TIMEOUT_S +
  slack`. Deploys must not 502 the requests already in the building.
- Docs (`/docs`, `/openapi.json`) on only when `environment == dev`.
- CORS from settings (no hardcoded `*` with credentials).
- **Proxy headers**: behind a LB, trust `X-Forwarded-For`/`X-Forwarded-Proto` via
  `ProxyHeadersMiddleware` + `FORWARDED_ALLOW_IPS` from settings — otherwise client IP is
  the LB and scheme is http in logs, auth, and redirects.
- Security headers (HSTS etc.), gzip, rate limiting: **gateway's job**, not the app —
  documented in README so nobody re-adds them here.

## Runtime

- Prod: **`uvicorn --workers N`** — uvicorn ≥ 0.30 ships its own multiprocess supervisor
  (respawns dead workers) and `uvicorn.workers` is deprecated upstream; gunicorn is gone
  (two fewer deps, one process model). Memory-leak recycling via `--limit-max-requests`.
- **Accepted trade, documented**: uvicorn has no hung-worker reaper. The per-request
  timeout (core layer, below) is the only guard against a blocked handler; a fully
  sync-blocked event loop is unrecoverable in-process. Mitigation in README: run 1–2
  workers per container and scale by replicas, so container-level liveness restarts cover
  a wedged process.
- `uvicorn[standard]` → uvloop + httptools. Dev: `uvicorn --reload`.
- **orjson everywhere**: `FastAPI(default_response_class=ORJSONResponse)`; orjson is the
  single JSON implementation — response bodies, obs envelopes (05), cache values. Native
  UUID/datetime/Decimal support deletes custom encoders.
- granian: evaluated and rejected — young project; reliability outranks peak RPS once DB
  work dominates.

## Settings

`src/config/settings.py` — pydantic-settings `Settings`, the single source of truth:

- **The earning rule**: an env var exists only if it must differ between deployments of
  the same code (URLs, secrets, `ENVIRONMENT`, modes/rates). Everything else is a named
  constant in source — template-first owners edit source. Tuning knobs (queue sizes,
  flush intervals, budgets, thresholds) are constants, not settings.
- Loaded once at import; **missing/invalid env fails boot with every problem listed at
  once**, including cross-field validation:
  `statement_timeout ≤ REQUEST_TIMEOUT_S < SHUTDOWN_GRACE_S` (see 02).
- `ENVIRONMENT` is an enum (`dev | staging | prod`) with **no default** — missing means
  boot failure. Every behavior switch derives from it, and every dev affordance
  (`AUTH_DEV_PRINCIPAL`, `/docs`, tracebacks in responses, `TRACE_MODE=dev`, open trace
  viewer) requires `environment == dev` **exactly** — never `!= prod`.
- Field descriptions are mandatory: they become the comments in the generated
  `.env.template` (07 — `sg config sync`; the template is *generated from* this class,
  never hand-edited, so it cannot drift).
- **Every plan file ends with a `## Settings` block naming exactly the fields it owns.**
  The template and `sg doctor` are generated from Settings, so a setting named only in
  prose is unbuildable. No plan may reference a setting outside some plan's block.

## Health endpoints

- `/livez` — process up, no dependency checks (restart signal for the orchestrator).
- `/readyz` — pings DB + Redis with its own short ~1s timeout (must pull the pod from
  rotation on pool exhaustion instead of hanging in the checkout queue — see 02).
  Separate on purpose: a dependency blip must pull the pod, not restart-loop it.
- `services/healthz/` stays as the example service; these two are framework routes.

## Service registrar (strict)

Same nomenclature as definable, hardened:

```python
class UserService:
  http_exposed = ["post=create", "get=list", "ws=stream"]

  def __init__(self, ctx: Context): ...
  async def post_create(self, data: UserCreate, session=Depends(get_db)): ...
```

- Recursive walk of `src/services/**/service.py`; folder path → URL path
  (`services/users/` → `/api/v1/users`); `*Service` class-name suffix discovery.
- Bare `get/post/put/delete` methods → service root.
- `ws=stream` → method `ws_stream` registered as websocket route (single pass — definable
  re-invoked ws registration per http route; fixed).
- STRICT validation at boot:
  - import error in any `service.py` → app refuses to start, full traceback
  - `http_exposed` entry without matching method → boot error
  - `{verb}_{path}` method without entry → startup warning
- **The generated endpoint wrapper commits the request session** (parked on
  `request.state` by `get_db`) after the service method returns and **before** the
  response is sent — handlers never call commit; commit failures surface as 500s, never
  as a silent "200 but rolled back" (see 02).
- Swagger tag from folder name; description from class docstring.
- One module loader (recursive), used by both registrar and Context — definable had two
  disagreeing implementations.

### Registrar decisions (resolve before build)

- **Service lifecycle**: one instance per request (default — no cross-request shared
  mutable state) unless `__init__` proves costly; revisit only with evidence.
- **Path params**: `http_exposed` entries support `get=detail/{id}` → method
  `get_detail(self, id: UUID, ...)`; placeholder ↔ parameter mismatch is a boot error.
- **Route metadata**: string entries can't express status codes (201 for create),
  `response_model` overrides, per-route dependencies, deprecation. Dict entry form is the
  escape hatch — `{"verb": "post", "path": "create", "status": 201}` — strings stay the
  90% path.
- **Service-level auth default**: class attr `auth = Auth()` applies to every route in
  the service; per-method dependency overrides it. Prevents the forgotten-endpoint hole.
- **API versioning**: everything mounts under `/api/v1` from day 1. A breaking change is
  a new mount, not new nomenclature.

No `mock.py` hot-swap (definable inheritance, unargued) — tests use FastAPI
`dependency_overrides`.

## API conventions

- **List envelope**: one pagination convention shipped —
  `{items: [...], total, limit, offset}` helpers + query-param dependency.
- **Request id**: echoed in the `X-Request-ID` response header, and injected into
  outbound calls made through `ctx.http`.

## Context

The exhaustive member registry — five one-word members, injected into every service
`__init__(self, ctx: Context)` and script `run(self, ctx)`:

| member | what |
|---|---|
| `ctx.settings` | the Settings object |
| `ctx.cache` | namespaced Redis cache (below) |
| `ctx.http` | shared outbound HTTP client (below) |
| `ctx.ws` | WebSocketManager |
| `ctx.lock` | PG advisory lock, async context manager (below) |

Deliberately absent:
- **No `ctx.logger`** — `from common.logger import log` is the one logging API (05);
  contextvars make it trace-bound everywhere, including code that has no ctx.
- **No `ctx.trace` / `ctx.obs` / `ctx.audit`** — tracing has zero author-visible surface
  (06's contract); business metrics and audit are `log.metric()` / `log.audit()` (05).
- **No `ctx.services`** — a sibling is `OrderService(ctx)`, one obvious line, no locator.
- **No db session** — sessions are request-scoped via `Depends(get_db)` only.

## Cache & locks

`common/cache.py`:

- Key convention: `{app}:{env}:{namespace}:{key}` built by the helper. **JSON (orjson)
  serialization only** — pickle is a deserialization attack surface. TTL is mandatory.
- `@cached(ttl=60, key=...)` decorator with **single-flight**: concurrent misses on one
  key → one computes, the rest await. Semantics locked: the computation runs in its own
  task detached from the initiating request (caller disconnect must not cancel the shared
  computation); waiters time out and fall through to computing themselves; exceptions
  resolve the shared future so waiters fail fast, not hang.
- **Redis-down degradation, defined once here**: cache reads fail open (error = miss),
  writes fail open (skip), both counted through the 05 pipeline, behind a tiny ~5s
  circuit breaker — a cache outage must not become a per-request connect-timeout latency
  storm. Same idiom as 05's "transport failures never propagate". 03's JWKS cache and
  06's kill-switch refresher reuse these rules.
- Explicit invalidation helper (`ctx.cache.invalidate(namespace, key)`).

`ctx.lock("reindex")` — distributed lock as an async context manager, **PG advisory only**
(one mechanism). The helper lives here in 01; 08's script runner reuses it.

## Outbound HTTP client

`common/http.py`: one shared `httpx.AsyncClient` as `ctx.http` — connection pooling,
**timeouts on by default** (connect/read/total constants; an untimed outbound call hangs
a worker), optional per-call retries for idempotent verbs, request-id + trace headers
auto-injected, every call a journey `http` step (06). App code never constructs ad-hoc
clients.

## The core ASGI layer (middleware, restructured)

Framework plumbing is **one fused pure-ASGI layer**, registered from `src/core/` in
`create_app()` — not discovered files. One send-wrapper and one `perf_counter` pair per
request instead of five, and a newcomer can't break tracing by renaming a file. It does,
in order:

1. **request_id** — creates trace context (06), binds request_id into the logger
   contextvar.
2. **RED counters** — increments in-process counters/histogram buckets keyed by
   (route template, method, status class); the 05 flusher emits periodic rollups. This
   layer *is* the owner of request RED data (04's `@task` wrapper owns task RED data).
3. **T0 journey bracket** — request start/end, status, duration (06).
4. **body limit** — max-bytes constant, counted incrementally while wrapping `receive`,
   never buffered; 413 on breach.
5. **request timeout** — per-request deadline `REQUEST_TIMEOUT_S`; on expiry records the
   504 to obs + journey **before** re-raising cancellation.

Exceptions are **handlers, not a layer**: `app.add_exception_handler` for `AppError` and
catch-all — standard envelope, traceback in dev responses only, error always recorded to
obs (05) + journey (06).

`src/middlewares/` ships **empty** — the documented escape hatch. Contract: pure ASGI
(`__init__(self, app)` + `__call__(scope, receive, send)`), auto-discovered in sorted
filename order, runs inside the core layer. **Subclassing `BaseHTTPMiddleware` is a boot
error**: it costs ~1.8x throughput and breaks contextvar propagation — the backbone 05/06
stand on.

**Idempotency is not a built-in.** Correct `Idempotency-Key` handling (atomic SET NX PX
reservation, running-marker TTL, body-hash binding, principal scoping, never-cache-5xx,
Redis-down stance) is 200+ subtle lines most clones don't need day 1. Ships as a working
copyable recipe file (BYO pattern, like 03's Stytch adapter) that drops into
`src/middlewares/` — the recipe carries that full requirement list as its spec.

## Cancellation contract

Client disconnects and request timeouts cancel the handler task. The consequences,
defined once:

1. `get_db` teardown catches `BaseException` (CancelledError is not `Exception`) →
   rollback, never commit.
2. The journey is finalized `status=cancelled` and emitted — disconnect storms must be
   visible.
3. Pending post-commit task submits are discarded and logged (04).
4. The timeout layer records the 504 before re-raising.

## Errors

`core/errors.py`: `AppError` hierarchy (kept from definable, trimmed) + one response
envelope `{error: {code, message, request_id}}`.

- **Stable error codes**: every `AppError` subclass declares a `code` (e.g.
  `order_not_found`). Codes are a registry — duplicate code → boot error.
- `sg errors export` (07) emits the catalog as JSON (code, HTTP status, message
  template) — the contract file frontends consume.

## WebSockets

`common/websocket.py`: connection manager (register/broadcast/close), `ws=` routes from
registrar. Auth on connect via the provider chain, token read from the
`Sec-WebSocket-Protocol` header — never the query string (03). Trace id propagates into
the ws session.

**Single-replica contract, stated loudly**: the in-process manager only reaches
connections on its own replica. That is the v1 contract — README + `WebSocketManager`
docstring say so, and `broadcast()` logs a one-time WARNING in prod. No backplane code
ships. (PG LISTEN/NOTIFY was evaluated and rejected as the future backplane — NOTIFY
serializes all commits through a global lock; see recall.ai's outage writeup. The
eventual add-on is Redis pub/sub, when a real multi-replica WS project needs it.)

## Settings (owned by this plan)

`ENVIRONMENT`, `REDIS_URL`, `CORS_ORIGINS`, `FORWARDED_ALLOW_IPS`, `REQUEST_TIMEOUT_S`,
`SHUTDOWN_GRACE_S`.
