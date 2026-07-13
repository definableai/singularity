# 09 — Observatory (first-party dashboard)

Status: building — the PROTOTYPE UI now ships verbatim and renders real data
(browser-verified): PG store, /__obs mount + auth + self-exclusion, proto assets +
vendored React (window.__resources CDN redirect), injected data patch (bootstrap sync,
trace/issue detail fetches, renderVals header vals), adapter with proto invariants
(≥1 issue/user, EXEC.fx1, ≥1 line per frame, QUERIES q1..qN), stat-tile + header
template rewrites, source lines via linecache into the code-trace panel. Verified in
real Chrome: overview tiles/live stream/throughput real; trace waterfall + code-trace
(args/returns/locals per line) real. Data views SHIPPED and browser-verified: guarded executor (read-only role via
DATAVIEWS_DB_URL, read-only txn, per-txn statement_timeout, single-statement prepare,
cursor row cap — never SQL rewriting), inference (pg type + semantic role, id-demotion,
cardinality), deterministic chart suggestion table, proto Run button wired through
renderVals patch (real inferRows/spec/rows), Save view button bound (was dead in proto)
→ /api/views → appears as qN tile. SQL-over-telemetry works (Logfire's feature, free).
Users via DASHBOARD_USERS_SQL shipped (guarded executor, falls back to principals).
Remaining: SSE into proto live stream (5s sync is the v1 mechanism), Redis pub/sub
tail, waterfall axis labels, duration rounding polish
Linked: consumes the [05-observability.md](05-observability.md) pipeline and store; renders
[06-tracing.md](06-tracing.md) journeys; tables live in the `singularity` schema owned by
[08-scripts.md](08-scripts.md)'s ensure_schema; commands in [07-dx.md](07-dx.md).
UI prototype: `plans/dasboard_ui_proto/` (Observatory.dc.html) — the design contract for
look and views.

Sentry + mini-Power-BI, shipped inside the template, mounted in the app, starts with the
server. Six views: **Overview · Logs · Traces · Issues · Users · Data views**. The dev
owns all of it — it's template code.

## Mount & auth

- Mounted at **`/__obs`** by `create_app()` (Phoenix LiveDashboard / mission_control
  pattern — routes in the app, not a second service). `/__trace` from 06 folds in as the
  Traces view; one dashboard, not two tools.
- **Auth is one overridable dependency**: `dashboard_auth`. Default: `environment == dev`
  → open on localhost; staging/prod → requires `DASHBOARD_TOKEN` (constant-time compare;
  enabling the dashboard outside dev without a token is a boot error). Projects override
  the dependency to hook their own auth/RBAC (mission_control's base-controller move).
- **Self-exclusion (the feedback loop)**: requests under `/__obs` are never traced, never
  RED-counted, never logged to the pipeline, and the store's own writes are invisible to
  capture. A dashboard that observes itself fills its own store — hard rule, tested.
- **The UI is the prototype, verbatim.** `plans/dasboard_ui_proto/Observatory.dc.html` +
  `support.js` (the dc runtime) are the shipped assets, copied to `src/obs/ui/` — not a
  reimplementation. No CDN, no build step (fonts degrade to system stack offline).

## Populating the prototype (the data contract)

The proto keeps all data as class-field constants on one `Component`
(`TRACES, SPANS, LOGS, USERS, QUERIES, EXEC, EXEC_TREE, ISSUES, STACK, CRUMBS, ERRS,
BARS, LIVE, FLAME, ALLOCS, TAGGROUPS, ISSUE_EVENTS`), and every view model is rebuilt
from them on each `setState`. So real data arrives by **instance-field replacement**,
not by rewriting their templates:

- A small injected patch script wraps `Component.prototype.componentDidMount`:
  fetch `/__obs/api/bootstrap` → `Object.assign(this, payload)` → `setState({})`.
  Re-sync every ~5s (their own tick interval stays for animations).
- `setState` is wrapped to detect `traceId` / `issueId` / `userId` changes → fetch
  `/__obs/api/proto/trace/{id}` (SPANS + EXEC + EXEC_TREE + CRUMBS),
  `/__obs/api/proto/issue/{fp}` (STACK + TAGGROUPS + ISSUE_EVENTS + spark), or user
  detail — assign, re-render. The proto's shapes ARE the API contract.
- Overview stat tiles are template literals in the proto — replaced server-side at page
  render (requests/min, p95, error rate, open-issue count) from RED rollups.
- Shape mapping (store → proto): journeys → `TRACES` (spans = steps+calls count);
  journey steps + call tree → `SPANS` waterfall (`kind`: sql→db, call→cpu, root→req)
  and `EXEC`/`EXEC_TREE` (args/ret/lines; **source text resolved via linecache** from
  the file:line the engine recorded — the proto shows code lines, we have the paths);
  issues → `ISSUES` (spark = 12-bucket 24h counts by fingerprint); exception-step
  traceback → `STACK` (in-app tag from code roots, context lines via linecache);
  trace-correlated logs → `CRUMBS` (relative ts, category from module); records
  principals → `USERS` until `DASHBOARD_USERS_SQL` is set; saved views → `QUERIES`.
- **Memory panels (FLAME/ALLOCS/heap tiles/mem columns) receive empty data** until 06's
  memory research track lands — panels render their empty states, never mocked numbers.
- SQL editor run button: patched to POST `/__obs/api/proto/query` through the guarded
  executor; result + inferred columns assigned back into the proto's `NEW_ROWS`
  pipeline.

## Storage: the obs store is Postgres

The "future obs backend" partially arrives now, as template code. 05's transports keep
fanning out (stdout for platform logs), but the **store** — what the dashboard reads — is
the `singularity` PG schema. Logfire's model validates this: one unified records table,
logs as zero-duration spans, SQL as the query interface. They started on Postgres too.

```sql
-- appended by every worker's flusher in batches; NEVER written in-request (Telescope's
-- documented death: sync per-request writes, 28M rows/day, DELETE-prune that can't keep up)
singularity.records (
  ts            timestamptz not null,
  kind          text,        -- log | journey | metric | audit
  level         text,        -- logs
  trace_id      text, request_id text, principal_id text,
  name          text,        -- route / task / script / logger name
  status        text, duration_ms int,
  fingerprint   text,        -- errors only → issues
  message       text,
  attributes    jsonb        -- everything else; capped ~8KB (PG TOASTs JSONB >2KB — hot
                             -- fields live in real columns for exactly that reason)
) PARTITION BY RANGE (ts);   -- native daily partitions (UTC)
```

- Indexes: **BRIN(ts)** (append-ordered, ~1/100 btree size), btree(trace_id),
  btree(fingerprint) partial `WHERE fingerprint IS NOT NULL`. Append-only — event rows
  are never UPDATEd.
- **Retention = partition drop**, never DELETE (constant-time, reclaims disk). A beat
  task (04) creates tomorrow's partition and drops those older than
  `OBS_RETENTION_DAYS`; `ensure_schema` (08) creates today+tomorrow at boot so a
  scheduler-less dev setup still works.
- Writer: the 05 flusher batch-INSERTs (multi-row, 1–2s cadence) over a dedicated small
  sync connection. **PG down → count + drop, never block** (05's transport discipline);
  the dashboard degrades to live-tail-only and says so in the UI.
- Volume guard is 06's emit policy: prod stores interesting journeys + sampled baseline +
  logs ≥ capture level + 10s RED rollups — not every request. Single-node PG with batched
  inserts is comfortable at 5–20k events/s; a boilerplate app emits orders of magnitude
  less.
- Supersedes last round's `TRACE_STORE=redis`: PG is the one store, trace lookup is
  `WHERE trace_id = $1`, and 06's readers point here. (Redis keys would have been a
  second store with no SQL.)

## Views

**Overview** — RED tiles (req/min, p95, error rate) from metric rollups; throughput
chart; unresolved-issues list; live log stream; pinned data view. Heap tile from the
proto: **phase 2**, blocked on 06's memory research track — panel hidden until real data
exists, never mocked.

**Logs** — search/filter over `records WHERE kind='log'` (level, service, time range,
principal, text); click a line → its journey via trace_id. Live tail below.

**Traces** — 06's viewer, embedded: journey list (path/status/duration filters), span
waterfall, call tree, per-line state and variable timeline where T1/T2 exists. The
proto's execution-path panel (args/locals/returns) is exactly 06's captured data.
Heap-during-request / GC / allocation flame graph: phase 2 (same gate as above).

**Issues** — Sentry's minimal credible subset:

```sql
singularity.issue (
  fingerprint   text pk,
  title         text,               -- "ValueError: negative total"
  state         text,               -- unresolved | resolved | ignored | regressed
  first_seen    timestamptz, last_seen timestamptz,
  event_count   bigint, user_count bigint,
  sample_trace_ids text[]           -- first + latest N (capped); events themselves live
)                                   -- in records — no second event table
```

- **Fingerprint** = sha256(exception type + in-app `module.function` frame chain) — no
  line numbers, so refactors don't split issues; in-app = under `TRACE_CODE_ROOTS`.
  `log.error(..., fingerprint="...")` overrides (Sentry's escape hatch).
- Grouping happens in the flusher (it already sees every error event): upsert the issue
  row, bump counts. **New event on a `resolved` issue → state flips to `regressed`** —
  the one lifecycle rule that matters; release-aware resolution is future.
- **Breadcrumbs are free**: Sentry needs a breadcrumb subsystem because it lacks tracing.
  We don't — the issue links trace_id → the full journey (logs, sql, http steps, line
  state) *is* the breadcrumb trail, better. No breadcrumb code ships.
- UI: grouped list with 24h sparkline, events/users, first/last seen; detail = stack
  trace + linked journeys + affected principals. Actions: resolve / ignore.

**Users** — reads the dev's own `public` schema via one configured mapping:
`DASHBOARD_USERS_SQL` (a SELECT returning id/email/name/created_at columns; unset →
view hidden). Detail joins `records` by principal_id for recent requests/errors.
Proto's impersonate/suspend are app-specific writes — **out of scope, documented**; the
extension point is the dev editing the dashboard code they own.

**Data views** — the Power BI half, below.

## Data views: SQL → result set → schema inference → view spec → UI

The proto's pipeline, made real:

1. **SQL editor** runs against the dev's DB through the guarded executor (below). The
   telemetry schema is queryable the same way — Logfire's headline feature ("SQL over
   your own traces") free because the store is PG.
2. **Schema inference**: pg types from the statement description + ~500 sampled rows →
   semantic roles — `time` (timestamptz/date), `measure` (numeric, not id-like),
   `dimension` (text/bool/low-cardinality), id-like columns demoted from measure
   (Metabase's two-layer lesson: mechanical type vs heuristic role, and **the role is
   user-overridable in the spec** — inference suggests, never dictates).
3. **Chart suggestion** — deterministic decision table (distilled CompassQL/Draco; no
   solver), with the "why" shown so devs trust it:

| result shape | chart | notes |
|---|---|---|
| 1 measure, 1 row | big-number | format from role (currency/%) |
| time + measure | line | area if cumulative, single series |
| time + measure + dimension (≤10 distinct) | multi-series line | high-card → table |
| dimension + measure | bar, sorted desc | >30 categories → top-N or table |
| 2 measures | scatter | >5k rows → sample |
| 2 dimensions + measure | grouped bar | both high-card → table |
| anything else | table | universal fallback, always offered |

Hard constraints: line/area need ordered x; bar needs discrete x; never line over nominal.

4. **View spec** — the saved artifact; UI re-renders from it on every run (never from the
   SQL text alone). Small, hand-editable, diffable (Evidence's lesson: spec is text):

```json
{
  "id": "revenue-by-day", "name": "Revenue by day",
  "query": {"sql": "SELECT ..."},
  "columns": {"day":     {"type": "timestamptz", "role": "time"},
              "revenue": {"type": "numeric", "role": "measure", "format": "currency:USD"}},
  "chart": {"kind": "line", "encoding": {"x": "day", "y": "revenue", "series": null}},
  "limits": {"rows": 1000}
}
```

- `columns` is the frozen inference snapshot: **re-validated on every run; drift
  ("column revenue disappeared") renders an error tile, never crashes the dashboard.**
- Stored in `singularity.view (id, name, spec jsonb, created_at, updated_at)`;
  `sg views export/import` round-trips specs to `views/*.json` in-repo — the DB is a
  cache of a text artifact, git is the source of truth for teams that want review.
- **Dashboards**: `singularity.dashboard (id, name, tiles jsonb)` — tiles are
  `{view_id, grid: {x,y,w,h}, refresh_s, overrides}`. Refresh lives **on the tile, not
  the view** (Grafana's separation — one view, many cadences); tile refreshes are
  jittered ±10% to avoid stampedes.

## The guarded SQL executor (defense in depth)

Every ad-hoc and saved-view query goes through one executor:

1. **Dedicated read-only role**: `sg db grant-readonly` generates
   `CREATE ROLE singularity_ro LOGIN ... ; GRANT CONNECT, SELECT ...;
   ALTER ROLE singularity_ro SET default_transaction_read_only = on;
   ALTER ROLE singularity_ro SET statement_timeout = '10s';` — the dev runs it and sets
   `DATAVIEWS_DB_URL`. **Unset → Data views and ad-hoc SQL are disabled**; obs views
   still work (grants slip; the role is the real wall — Metabase/Grafana both punt here
   and say so).
2. Separate tiny pool (2 conns) — dashboard queries can never exhaust the app pool.
3. **Single-statement execution** via asyncpg `prepare()` (extended protocol rejects
   multi-statement strings natively — no semicolon regexing).
4. **Row cap by fetch, not SQL rewrite**: `fetchmany(limit+1)` → `truncated: true` flag;
   wrapping arbitrary SQL in `SELECT * FROM (...) LIMIT n` breaks on CTEs/comments and
   changes plans. Cap 10k ad-hoc, `limits.rows` for saved views.
5. `idle_in_transaction_session_timeout` + `lock_timeout` on the role. EXPLAIN cost
   gating: skipped v1 (nobody mainstream ships it; timeouts are the accepted control).
6. Saved views are the allow-list analogue: tiles only run vetted specs; raw SQL stays
   behind `dashboard_auth`.

## Live tail (Logs/Traces streams, Overview ticker)

- **SSE, not WebSocket** — strictly server→client, plain HTTP through any proxy/auth
  middleware, `Last-Event-ID` reconnect built into EventSource, curl-testable. Filter
  changes = reopen the URL with new query params.
- Fan-in: multi-worker uvicorn means the browser connects to ONE worker but events happen
  in ALL. Each flusher PUBLISHes to a Redis channel alongside its PG batch; the SSE
  endpoint subscribes and relays, filtering server-side. Lossy on disconnect —
  acceptable for a live tail; history is the store.
- **Redis absent → 2s PG polling fallback** (same SSE endpoint, different source). PG
  LISTEN/NOTIFY rejected for the firehose (global commit lock — same reason as 01's WS
  decision).
- SSE connections capped (constant, ~10) — it's a team dashboard, not a product surface.

## Edge cases (owned)

- **Feedback loop**: self-exclusion above; tested (a dashboard request must produce zero
  records).
- **Store bloat**: partition drop + attrs cap + emit-on-interesting. `sg doctor` warns
  when the schema exceeds a size threshold.
- **PG store down**: writes counted + dropped (never block serving); UI banner "store
  unavailable — live tail only".
- **Redis down**: live tail falls back to polling; everything else unaffected (store is
  PG).
- **Worker crash**: in-flight buffer lost (≤ a batch) — obs is lossy-by-design under
  failure; the audit rule stands (must-never-lose facts → app PG rows, 05).
- **Issue-group explosion** (unbounded fingerprint cardinality — e.g. ids interpolated
  into exception messages): title from exception *type + location*, not message;
  `sample_trace_ids` capped; issues list paginated; worst case is many rows in one small
  table, not event duplication.
- **View drift**: column re-validation per run → error tile.
- **Refresh stampede**: jittered tile refresh; per-tile `refresh_s` floor (10s).
- **Query runaway**: role-level statement_timeout is the backstop even if the app-side
  timeout is bypassed.
- **Secrets**: telemetry is redacted upstream (05/06); data-views output is the dev's own
  DB behind `dashboard_auth` + read-only role; dashboard token never logged.
- **Partitions and clocks**: UTC everywhere; boot ensures today+tomorrow exist (no
  midnight gap if beat is down).

## Settings (owned by this plan)

`DASHBOARD_TOKEN`, `DASHBOARD_USERS_SQL`, `DATAVIEWS_DB_URL`, `OBS_RETENTION_DAYS`.
Mount path, SSE cap, row caps, sample sizes: constants (01's earning rule).

## Build slices

1. PG store: records table + partitions + flusher batch-writer + retention task
2. Dashboard shell: mount, auth dependency, self-exclusion, static assets from proto
3. Logs + Traces views (reads store; 06 viewer embeds), SSE live tail (Redis + polling
   fallback)
4. Issues: fingerprint in flusher, issue table, list/detail/resolve/ignore/regressed
5. Data views: guarded executor, inference + decision table, view spec renderer, save
6. Dashboards/tiles + pinning + Overview composition; Users view; `sg views
   export/import`, `sg db grant-readonly`
