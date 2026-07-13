# 06 — Request-journey tracing (first-party)

Status: building — T0 + engine shipped: trace context, Journey model, CoreLayer
bracket, endpoint/exception/sql steps, emit-on-interesting; sys.monitoring engine live
(tool-id claim with fallback, global call events gated by armed refcount, frame-identity
tree — async-safe by construction, PY_UNWIND exception closes, T1 args/returns, T2
f_locals line diffs with capped serializers, budget degrade ladder, circuit breaker,
LINE self-extinguish + restart_events revive). Verified live: plain service code →
call tree + line state in PG. N+1 detector shipped (statement-shape fold at finish, flags journey,
emit-worthy under on_error) + `sg replay` shipped (re-fires recorded method/path/query/
body — T0 now captures query string always and armed non-GET bodies capped 8KB;
credentials never stored, --yes gate for non-local non-GET; verified live). Redis kill switch shipped (background refresher, ~2s effective, verified live) +
backpressure coupling shipped (queue depth sheds T2→T1→T0). http + task_submit steps live. Complete for v1 (memory capture, trace-to-test,
journey diff, deterministic replay remain research track by design).
Linked: journey records ship through the [05-observability.md](05-observability.md)
pipeline (`kind: "journey"`) and are read back from the PG store (09).

Goal: record the **full journey of a request** — request → core layer → dependencies →
endpoint → **every user function it calls, transitively** — including per-line variable
state, **fully automatically**. A dev writes a plain endpoint; the recording is just there.
Nothing to add, nothing to remember. This is the DX contract:

```python
async def post_create(self, data: OrderCreate, user=Depends(Auth()), session=Depends(get_db)):
  order = build_order(user, data)          # ← recorded, inside too
  await self._price(order, session)        # ← recorded, inside too
  await self.payments.charge(user, order)  # ← recorded, and its callees too
  return OrderOut.model_validate(order)
# no decorators, no snap(), no ctx.trace anything
```

Not OpenTelemetry. Custom, because the unit we care about is the journey with state
diffs, not vendor-neutral spans. (OTel export adapter possible later.)

## Trace model

```
Journey (trace_id, request_id, method, path, principal, duration, status)
└── Step (ordered tree)
    ├── kind: middleware | dependency | call | line | sql | http | task_submit | exception
    ├── name, started_at, duration
    ├── data      # kind-specific payload (redacted, size-capped)
    └── children
```

`call` steps form the **call tree**: endpoint → a() → b() → c(), any depth. One journey
per request; also one per Celery task execution and per WS session (linked via
`parent_trace_id`).

## Engine: `sys.monitoring` (PEP 669, Python ≥ 3.12 — pinned)

Why not `sys.settrace`: global, 10–100x tax on every frame. Why not import-time AST
rewriting: permanent overhead even when tracing is off, import-hook fragility.
`sys.monitoring` gives per-code-object events and **zero overhead when unarmed** — no
armed journeys → events disabled → the interpreter never calls us. (Honest wording:
zero when *unarmed*; armed costs are the tier table below.)

Engine facts the spec is built on (per CPython docs):

- **Local events** (`set_local_events`, per code object): `PY_START`, `PY_RETURN`,
  `PY_YIELD`, `PY_RESUME` (+ `LINE` at T2). These drive the call tree and flight
  recorder.
- **Exception events are GLOBAL-only** — `RAISE`, `RERAISE`, `PY_THROW`, `PY_UNWIND`,
  `EXCEPTION_HANDLED` cannot be set per code object, and `PY_RETURN` does not fire on
  exception exits. So: exception events are registered via `set_events()` and toggled on
  only while the armed-journey refcount > 0, filtered through the cached code-object
  classification. Without this, failed requests — the tracer's whole point — would
  produce corrupted call trees and leaked frame state.
- **Stack unwinding pops by frame identity, not by count** — a budget disarm mid-request
  must not leave dangling frames in the recorded tree.
- **Callbacks are provably harmless**: every callback body is wrapped catch-all — an
  internal tracer error increments a counter and trips the circuit breaker; it can never
  alter app behavior.
- **Tool id**: claim a free id (3 or 4 — 0/1/2 are conventionally debugger, coverage,
  profiler). `ValueError` on conflict → run T0-only + one loud boot line; otherwise
  `pytest --cov` would collide with the failing-test journey dump (07).

### Automatic transitive discovery (no registration, no decorators)

1. Registrar arms the endpoint method's code object at route-build time.
2. PY_START fires for every function the traced code calls. First sight of a code
   object: classify by `co_filename` —
   - under `TRACE_CODE_ROOTS` → **user code**: arm it (calls + lines per tier), record it
   - anything else → ignore, never armed (its effects are already visible as sql/http/task steps)
3. Classification cached per code object. `a()`, `b()`, `c()` — and their callees — enter
   the recording the first time they run, to `TRACE_MAX_DEPTH` (constant, 20).

**`TRACE_CODE_ROOTS` default: `src/services, src/models, src/tasks, src/scripts`** — the
four directories the generators target. The framework's own plumbing (core, obs, cache,
auth internals) is deliberately outside: it is already captured as T0 boundary steps, and
re-recording it would be noise + wasted budget. src/tasks and src/scripts stay in the
default so 04's task journeys and 08's "line-level debugging of a failed backfill" remain
true. When an armed journey calls src/ code outside the roots, the classifier emits a
one-time-per-code-object T0 note (`unarmed_user_code`, co_filename) and the viewer shows
a banner — coverage gaps are loud. Added a new top-level src/ dir? Add it to the roots.

### What gets captured per tier

| tier | events | recorded | armed cost (typical endpoint) |
|---|---|---|---|
| **T0 journey** | none (framework hooks only) | core layer, deps, payload/return, sql, http, task submits, exceptions | ~0 — always on, every request |
| **T1 calls** | PY_START/PY_RETURN on user code | full call tree: function, **args in, return value out**, duration, exceptions per frame | ~1–5µs per call |
| **T2 lines** | T1 + LINE on user code | per line: changed variables (f_locals diff vs previous line), per-line duration | ~5–20µs per line |

T1 alone answers "what did b() receive and return". T2 is the full flight recorder.

State capture detail (T2): frame entry captures **all arguments**; each line stores only
the diff vs that frame's previous line. The viewer reconstructs full in-scope state at
any line by folding entry args + diffs; storage stays proportional to what changed.
Serializers are type-dispatched with hard caps (immutables verbatim; str truncated;
containers → first N + len; Pydantic/ORM → registered summarizers; else capped repr).
Known honest limit: in-place mutation of a shared object shows up when the container's
summary changes, not as a rebind — documented; the container fingerprint (type+len+head)
catches the common cases.

Per-line durations fall out of LINE timestamps → the slow line is visible in the same
view (poor-man's line profiler included).

## Robust under heavy load (the production story)

1. **Arming decision is per-request, at request start.** Unarmed request → zero events.
   Concurrent armed + unarmed on the same endpoint: unarmed pay only a contextvar check
   (~100ns/event) while any armed journey exists on that code object.
2. **Budgets per journey** (constants): max events 5,000, max captured bytes 256KB, max
   depth. Budget hit → tier drops for the rest of the journey (T2→T1→T0), journey marked
   `degraded`, never silently.
3. **Adaptive value degradation**: a variable whose capture repeatedly exceeds its
   per-value time budget is demoted to `{type, len}` for the rest of the journey.
4. **Backpressure coupling to 05**: obs queue depth over high-watermark → shed T2 first,
   then T1, keep T0. Recording never competes with serving traffic.
5. **Circuit breaker**: sustained tracer overhead above threshold (self-measured) →
   auto-disarm line capture process-wide, loud log event. **Manual kill switch**: a Redis
   flag polled by a background refresher into a process-local variable (~2s effective) —
   never a per-request GET; Redis down = last-known value (01's degradation rules).
6. Serialization happens at capture time (values mutate later — must copy); enrichment +
   shipping are async via the 05 flusher.

## T0 emission: capture always, emit when interesting

A 1–3KB T0 journey per request is ~2–3MB/s at 1k RPS — mostly recording healthy requests
nobody will read. T0 capture stays always-on in memory; **emission** is decided at
request end, with full information:

- dev → emit all.
- prod → emit when *interesting*: status ≥ 500, unhandled exception, duration >
  `TRACE_SLOW_MS`, N+1 flag, X-Trace header, or the `TRACE_SAMPLE_RATE` baseline (keeps
  healthy journeys available for comparison/replay).

RED rollups (05) cover the non-emitted majority: every request measured, every
interesting one fully journaled.

## Arming modes (`TRACE_MODE`)

| mode | behavior | use |
|---|---|---|
| `dev` | every request at T2, all user code | dev default — recorded debugger always on |
| `on_error` | T2 captured into an in-memory ring for `TRACE_SAMPLE_RATE` of requests; **emitted only if the request fails or exceeds `TRACE_SLOW_MS`**, else discarded | prod default — pay capture on the armed %, pay storage only for requests worth reading |
| `off` | T0 only | minimal |

Defaults derive from `ENVIRONMENT` (dev→`dev`, staging/prod→`on_error`); settings
validation **refuses `TRACE_MODE=dev` outside dev** (01's dev-affordance rule). The
auth-gated `X-Trace: lines` header is always honored — targeted prod debugging is a
standing capability, not a mode. One knob: `TRACE_SAMPLE_RATE` (the on_error arming %).
The prod story in one sentence: *on_error at N%, header for targeted debugging.*

## Security (trace data is a PII/secret store)

T1 records args/returns; T2 records locals. Field-name lists miss secrets in innocuous
locals (`t = headers["authorization"]`), so the protection is layered:

1. **Sensitive-by-default**: trace payloads are classified like the prod DB — same access
   class, never world-readable.
2. **Credential strip at capture time**: denylist (authorization, cookie, x-api-key, …)
   applied when the value is serialized — otherwise the store is a session-token dump and
   any later stripping is theater.
3. **Value-pattern scrubbing at serialize time** (~30 lines in the existing serializer):
   `eyJ` JWTs, `Bearer `, PEM blocks, `sk_`/`AKIA` key shapes, PAN+Luhn.
4. **Per-endpoint capture opt-out**: login / password-reset / webhook handlers are never
   T1/T2-armed (declared in `http_exposed` dict form).
5. **Access control**: 03 ships no RBAC, so "auth-gated" would collapse to "any valid key
   reads every user's recorded state". Viewer access and X-Trace arming are gated by
   Observatory's `dashboard_auth` dependency + `DASHBOARD_TOKEN` (09) — one gate for all
   recorded state. Field redaction list: `OBS_REDACT_FIELDS` (05) — one owner.
6. **Replay auth**: replay never stores or replays credentials; it re-authenticates via
   `--auth <token>` or targets a local instance using `AUTH_DEV_PRINCIPAL`. Non-GET
   replay against a non-localhost base requires `--yes`.

## DX

- **Zero code in endpoints.** Write the function; the journey exists.
- Optional (never required): `log.info("priced", total=total)` — log lines land inside
  the journey at the right spot automatically (they carry trace_id), so labeling a
  business moment is just logging.
- Viewer = **Observatory's Traces view** (09) at `/__obs`, reading from the PG store —
  no separate `/__trace` app. Same panels:
  - waterfall + **call tree** — every user function a span
  - click a function → source with per-line hit counts + durations in the gutter; click a
    line → variables after it, diff-highlighted
  - **variable timeline**: pick a variable, see its evolution across lines and functions
  - exception view: traceback where every frame expands to its recorded state
  - N+1 banner when flagged
- `sg trace <id> [--lines]` — same journey in the terminal.
- Failing tests dump their journey automatically (07).

## Built on the recording

Two derivative tools ship in v1 — both cheap, both high-certainty:

- **N+1 / repeated-query detector**: fold the journey's sql steps by statement shape
  (params stripped); same shape ≥ threshold (constant, 10) in one journey → flag
  `n_plus_one`, viewer banner, obs event, and the flag makes the journey emit-worthy in
  prod. ~50 lines over existing spans; also marks the slowest query and total SQL time
  share.
- **Request replay**: `sg replay <trace_id> [--base http://localhost:8000]` — re-fires
  the recorded request (method, path, headers minus credentials, payload). Replayed
  requests carry `X-Replay-Of: <trace_id>`; their journeys link back. Auth per the
  security section.

Moved to the future research track (below): trace-to-test (volatile-field heuristics make
generated tests flaky by construction, and recorded prod payloads verbatim in committed
test files are the most durable leak in the design) and cross-journey diff (tree
alignment is 300+ lines of the least-certain code; two viewer tabs cover 80%).
TRACE_MEMORY/tracemalloc is cut outright: tracemalloc is process-global, so arming one
request taxes every concurrent request — it violates this plan's own zero-overhead
guarantee.

## Future research track (not v1)

- **Deterministic replay**: T0 already records every boundary input. Re-executing offline
  against recorded inputs would reconstruct full line-level state with zero hot-path
  capture — and would subsume trace-to-test properly. Blocked on non-determinism control.
- Trace-to-test, cross-journey diff (see above).

## Settings (owned by this plan)

`TRACE_MODE`, `TRACE_SAMPLE_RATE`, `TRACE_SLOW_MS`, `TRACE_CODE_ROOTS`.
Budgets, depth, N+1 threshold: constants (01's earning rule). Store and viewer access
moved to 09 (`OBS_RETENTION_DAYS`, `DASHBOARD_TOKEN`).

## Build slices

1. Trace context + journey/step model + T0 (core-layer/dep/endpoint boundaries, sql,
   http, task) + jsonl emit
2. Engine: sys.monitoring arming (local events + global exception events + tool-id
   claim), code-object classifier, T1 call tree
3. T2 line capture: f_locals diff, serializers + scrubbing, budgets, degradation ladder
4. Dev viewer: journey list, waterfall + call tree, source panel, variable timeline
5. Modes (`on_error` ring buffer, emit-on-interesting), circuit breaker, kill-switch
   refresher; readers point at the PG store (09)
6. Celery/WS journeys + parent linking
7. Recording-powered tools: N+1 detector, replay
