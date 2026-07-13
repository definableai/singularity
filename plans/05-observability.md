# 05 — Observability (first-party)

Status: building — foundation shipped (capture sink, ≤5µs enqueue, ring + flusher thread,
jsonl/stdout/null transports, redaction on flusher, stdlib bridge incl. uvicorn,
log.metric/log.audit, bounded shutdown flush, PG store writer + issue fingerprint
upsert + regressed lifecycle, pre-aggregated RED rollups (request + task), retention
beat task); redis_stream transport shipped (XADD, MAXLEN-capped). Remaining: live-tail redis
publish (09 — PG-poll SSE covers it today)
Linked: [06-tracing.md](06-tracing.md) shares the same pipeline; journey records are one
event kind flowing through the transport described here.

Goal: **every** `log.info/debug/error/...` call anywhere in the app (API and worker) is
captured as a structured event and shipped through our own pipeline. No third-party obs
provider in the core. When we later build our own obs backend, it plugs in as just
another transport — zero call-site changes.

## Event envelope

Every capture — log line, journey record, metric rollup, audit fact — normalizes to one
schema:

```jsonc
{
  "v": 1,
  "kind": "log" | "journey" | "metric" | "audit",  // one pipeline, four event kinds
  "ts": "2026-07-12T10:31:04.123456Z",
  "level": "INFO",                              // logs only
  "message": "user created",
  "logger": {"module": "services.users.service", "line": 42, "function": "post_create"},
  "ctx": {
    "request_id": "req_...",                    // from contextvar, auto
    "trace_id": "trc_...",                      // journey correlation (06)
    "service": "users",                         // owning service, when resolvable
    "task": "emails.send_digest",               // worker side
    "principal_id": "usr_..."                   // when authenticated
  },
  "env": {"app": "myapp", "environment": "dev", "host": "...", "pid": 123},
  "extra": { ... }                              // structured kwargs from log call
}
```

Versioned (`v`) so the future obs backend can evolve the schema.

## Capture (hard ≤5µs at the call site)

- Loguru is the only logging API app code uses: `from common.logger import log`. It also
  carries the two non-log verbs — `log.metric("name", n)` and
  `log.audit("order.refunded", entity="order:123", amount=50)` — one author-facing
  surface, four event kinds.
- **The call-site budget is ≤5µs**: read contextvars + shallow-copy `extra` + append a
  tuple to the bounded deque. Envelope assembly, redaction, and orjson encode all happen
  on the flusher thread. Honest limit (documented like 06's mutation caveat): shallow
  copy stabilizes top-level bindings; nested mutables are snapshotted at flush time, not
  call time.
- The flusher encodes each event defensively: per-event try/except with a capped-repr
  fallback and an `encode_errors` counter — a concurrently mutated container must never
  crash the flusher or silently lose the event.
- **Loguru pinned config**: (1) `enqueue=False` on every sink — loguru's enqueue pickles
  records through a multiprocessing queue, strictly worse than our ring+thread, and
  raises on unpicklable extras. (2) Prod runs the capture sink ONLY — no console sink
  (the stdout transport already emits JSON; a second sink doubles per-call formatting and
  doubles collector volume). Dev keeps pretty console + capture. (3) `backtrace=False,
  diagnose=False` in prod — diagnose reprs frame locals per exception: slow AND leaks
  secrets past the redactor.
- Capture level (dev DEBUG, prod INFO — derived from `ENVIRONMENT`) is applied as the
  loguru sink level, so filtered DEBUG calls cost ~a function call. `log.audit()`
  enqueues directly, bypassing the level filter.
- Stdlib-`logging` bridge: `InterceptHandler` routes third-party logs (sqlalchemy,
  celery, uvicorn) into the same pipeline.
- Same setup in API and worker — one `obs.init()` called by both entrypoints.

## Buffer & flush (never block, never die silently)

- Bounded in-memory ring queue (size constant). Enqueue is O(1) and non-blocking.
- Background flusher thread drains in batches (N events or T ms — constants). Per batch
  it: writes transports, batch-INSERTs the PG store, upserts issue rows for error events
  (fingerprinting, 09), and PUBLISHes to the live-tail Redis channel (09) — one drain
  loop, four sinks, all failure-isolated.
- **The flusher cannot die from a batch failure**: the loop body is wrapped in a
  catch-all inside the `while` — an exception in one iteration (transport bug,
  serialization bug) logs to stderr and continues.
- **Loss reports out-of-band**: `dropped_events` and flusher-error counts are reported
  through the pipeline AND written to stderr periodically — reporting pipeline loss only
  through the pipeline itself is circular.
- Overflow policy: drop-oldest + count. Silent loss is not acceptable; blocking the app
  less so.
- **Bounded shutdown flush**: stop accepting → drain ≤ `SHUTDOWN_GRACE_S` → final flush
  ≤ `OBS_FLUSH_TIMEOUT_S` (default 5) → join with timeout → exit. A down transport at
  shutdown (exactly when things are broken) must not hang until SIGKILL. Same bounded
  flush on Celery `worker_shutdown` (04). README states the k8s grace math (01).

## Transports (pluggable, configured in settings)

```python
class ObsTransport(Protocol):
  def send(self, batch: list[bytes]) -> None: ...   # orjson-encoded, called from flusher
```

| transport | use | notes |
|---|---|---|
| `jsonl` | dev default | append to `logs/events.jsonl`, size-rotated; greppable, zero deps |
| `stdout` | **prod default** | one JSON event per line to stdout → the platform's log collector (Loki / CloudWatch / Datadog). Logs readable in prod from day 1, before our obs backend exists |
| `redis_stream` | prod add-on | `XADD obs:events` with MAXLEN; durable buffer the future obs backend consumes. MAXLEN is the retention policy (`OBS_STREAM_MAXLEN`) |
| `http` | future | batch POST to our obs ingest API |
| `null` | tests | discard |

orjson returns bytes; transports write bytes + newline. Fan-out allowed
(`OBS_TRANSPORTS=["stdout", "redis_stream"]`). Transport failures never propagate — log
to stderr, count, keep serving.

**Prod story before our backend exists**: stdout → platform collector for search;
**alerting = the platform's ERROR-rate alert on that stream** (CloudWatch/Loki/Datadog
all do dedup/routing/on-call natively — one README line, no webhook code of ours);
redis_stream accumulating for the day our backend lands. A jsonl file inside a container
is a dead end and is dev-only.

## The store (read path): Postgres

Write transports alone would leave every reader blind in prod — stdout is write-only
into a collector. The store is the `singularity.records` PG table, defined and owned by
[09-dashboard.md](09-dashboard.md): the flusher batch-INSERTs every event kind there
(daily partitions, partition-drop retention, append-only), Observatory and 06's readers
query it, and it doubles as SSE live-tail history. PG down → count + drop, never block.
This supersedes the earlier `TRACE_STORE=redis` design — one store, addressable by SQL.

## Redaction & retention

- **One redactor, one owner**: `OBS_REDACT_FIELDS` (password, token, authorization,
  cookie, api_key, …) applies to the whole pipeline — log `extra` and journey payloads —
  at envelope-build time on the flusher thread. 06 adds value-pattern scrubbing for
  trace-captured state; the field list is this one.
- **Retention is a setting, not an accident**: jsonl rotation caps (constant),
  `OBS_STREAM_MAXLEN`, `OBS_RETENTION_DAYS` (09 — store partition drop). README states
  what is kept where, how long.

## Metrics (`kind: "metric"`)

- **Auto-RED, pre-aggregated**: 01's core layer (requests) and 04's @task wrapper (tasks)
  increment in-process counters/histogram buckets keyed by (route, method, status class).
  The flusher emits **one rollup event per active route per ~10s window** — not one event
  per request, which at 1k RPS would be 86M events/day of inherently aggregate data.
  100–1000x volume cut, identical dashboards. Manual `log.metric()` is for business
  numbers only.
- No Prometheus dependency in core; a `/metrics` exporter can be a later add-on reading
  the same counters.

## Audit (`kind: "audit"`)

`log.audit("order.refunded", entity="order:123", amount=50)` — principal id, request_id,
trace_id attach automatically from contextvars; the call site states only the business
fact. It is a ~10-line emitter with **the same queue semantics as every other event** —
no priority slot, no blocking enqueue (a second queue discipline would break the
never-block rule while selling false durability: an in-memory ring flushed to stdout is
not compliance-grade). README states it plainly: audit durability = log-platform
retention; if "who refunded what" must **never** be lost, that's a Postgres row (the same
pattern 08 uses for script runs).

## The obs backend is Observatory (09)

The earlier "future obs backend, separate project" is superseded: storage (PG store),
query surface (SQL + views), and UI ship in-template as Observatory
([09-dashboard.md](09-dashboard.md)). `redis_stream` remains for anyone fanning out to
external systems; the envelope schema stays versioned as the contract.

## Settings (owned by this plan)

`OBS_TRANSPORTS`, `OBS_REDACT_FIELDS`, `OBS_STREAM_MAXLEN`, `OBS_FLUSH_TIMEOUT_S`.
Queue size, flush interval, batch size, rotation caps: constants (01's earning rule).
