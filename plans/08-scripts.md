# 08 — Scripts & framework database

Status: building — shipped: ensure_schema (advisory-locked, all singularity.* tables +
records partitions, meta version), BaseScript + strict discovery, once/repeatable/manual
semantics with checksum drift detection, advisory-locked runner with running-row
recording + journey per run, dev startup hook. Heartbeat shipped (running row refreshed 5s; stale heartbeat = dead runner);
sg script CLI + retention beat task shipped. Remaining: lock-loss abort (deferred —
advisory-lock release on connection death already prevents double-run)
Linked: runs are traced via [06-tracing.md](06-tracing.md), logged via
[05-observability.md](05-observability.md); command surface in [07-dx.md](07-dx.md);
advisory-lock helper from [01-core.md](01-core.md).

Operational code that isn't a request handler and isn't scheduled work: backfills, seeds,
data migrations, one-off fixes, rebuilds. First-class in Singularity, recorded in the
database.

**Boundary rule**: runs on a schedule → Celery beat task (04). Runs because a human or a
deploy decided → script. One mechanism each, no overlap.

## Script kinds

| kind | semantics | identity | example |
|---|---|---|---|
| `once` | exactly one successful run per environment, ever | numbered filename `0001_seed_roles.py`, ordered like migrations | backfill, seed, data migration |
| `repeatable` | reruns when file checksum changes | filename | rebuild search index, sync plan catalog |
| `manual` | only via explicit `sg script run <name>`; every run recorded | filename | fix_duplicate_orders |

```python
# src/scripts/0002_backfill_totals.py
class Script(BaseScript):
  kind = "once"                     # once | repeatable | manual
  description = "Recompute order.total for pre-v2 rows"

  async def run(self, ctx: Context) -> dict:      # return value stored as run output
    ...
```

**Forward-only — no rollback().** Real backfills have no true inverse, and the plan is
already forward-only for schema (02). To undo a script, write the next script (its own
run row, checksum, journey).

Discovery: `src/scripts/*.py` walked at CLI/boot time, class named `Script`, same
strict-loudness as the service registrar (bad import = error, not skip). The directory
contains **only** BaseScript files — the CLI lives in `src/cli/` (07).

## The framework schema: `singularity.*`

Postgres is a hard dependency. The framework owns a dedicated PG schema, separate from
the app's `public` schema (which alembic owns). This plan owns the whole schema story.

`ensure_schema()` — `CREATE SCHEMA IF NOT EXISTS` + `CREATE TABLE IF NOT EXISTS`, the
whole thing wrapped in `pg_advisory_lock(constant)` because concurrent `IF NOT EXISTS`
is **not** race-safe in PG (duplicate-key on `pg_namespace` under multi-replica boot).
Runs at **app boot AND CLI start**: 07's deploy contract writes `script_run` via the CLI
before any pod boots, so boot-only creation would break fresh prod deploys.

Tables it ensures: `script_run` (below) + Observatory's tables
([09-dashboard.md](09-dashboard.md)): `records` (partitioned parent + today/tomorrow
partitions), `issue`, `view`, `dashboard`. 09 defines those shapes; 08 owns the creation
mechanism. Simple version int in `singularity.meta` now that the schema has multiple
tables.

v1 table:

```sql
singularity.script_run (
  id            uuid pk,
  name          text,              -- "0002_backfill_totals"
  kind          text,              -- once | repeatable | manual
  checksum      text,              -- sha256 of file at run time
  status        text,              -- running | success | failed
  started_at    timestamptz,
  finished_at   timestamptz,
  duration_ms   int,
  error         text,
  output        jsonb,             -- run()'s return value
  triggered_by  text,              -- startup | cli | deploy
  forced        bool,
  host          text, pid int,
  trace_id      text               -- → journey (06): failed backfill = flight recorder
)
```

Future framework tables land in the same schema — one namespace for everything
Singularity manages about itself.

## Execution semantics (the correctness part)

- **Pending resolution**: a `once` script is pending iff no `success` row for its name.
  `repeatable` is pending iff latest success row's checksum ≠ current file checksum.
- **Concurrency**: run wrapped in `ctx.lock` (01's PG advisory helper, keyed on script
  name). 4 uvicorn workers × N replicas → exactly one executes; the rest wait, re-check
  the row, skip. DB is the source of truth, never process-local state.
- **Lock-loss abort**: the runner heartbeats its `running` row and aborts loudly if the
  advisory-lock connection dies — a PG failover must not let two replicas both conclude
  they hold the lock.
- **Ordering**: pending `once` scripts run in filename-number order; a failure stops the
  queue (later scripts may depend on earlier ones).
- **Drift detection**: `once` script whose file checksum ≠ its success-row checksum →
  hard error at boot ("0002 was edited after it ran"). Edits after apply are lies.
- **Failure & retry**: `failed` row doesn't count as done — next `--pending` run retries.
  `--force` reruns a completed `once` script; recorded as a new row with `forced=true`.
- **When pending scripts run** (derived from `ENVIRONMENT`, no setting): dev →
  automatically at startup (lifespan hook). staging/prod → explicit deploy step
  `sg script run --pending` (same policy as migrations; boots shouldn't mutate data
  implicitly).
- **Recording is total**: `running` row inserted before execution (crash leaves visible
  evidence), updated on finish. Every run is a journey (06) with `script:<name>` root —
  line-level debugging of a failed backfill for free; logs flow through obs (05).

## CLI (surface defined in 07)

`sg script run <name> [--force]` · `sg script run --pending` · `sg script ls` ·
`sg script history <name>` · `sg g script <name> --kind once`

## Kept vs changed from definable

Kept: BaseScript class shape, run-tracking-in-DB idea, Click CLI, generator.
Changed: kinds model (definable had ad-hoc `auto_run` flag), dedicated `singularity`
schema (definable wrote into the app schema), advisory-lock concurrency via 01's shared
helper (definable had none — multi-worker startup could double-run) + heartbeat/lock-loss
abort, checksum drift detection, forward-only (rollback() dropped), journey/obs
integration, prod-explicit startup policy.

## Settings (owned by this plan)

None — startup policy derives from `ENVIRONMENT` (01's earning rule).
