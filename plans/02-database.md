# 02 — Database

Status: building — shipped: async engine (asyncpg, pool_timeout 5s, pre_ping,
statement/lock timeouts via connect args, pgbouncer mode flag), get_db +
commit-before-response via registrar wrapper, BaseException-safe teardown, 503
`db_unavailable` mapping, Base/UUID/Timestamp/SoftDelete mixins, model autodiscovery,
async alembic (URL from env), sql journey steps, compose PG/Redis + /readyz +
fail-fast boot pings. Sync engine shipped (get_sync_session, psycopg v3, sql journey steps); pool math in README. Complete for v1.

## Engines

Dual-engine split (kept from definable — it's correct):
- **async** (asyncpg) for API: `AsyncAdaptedQueuePool`, `pool_pre_ping=True`,
  **`pool_timeout` ~5s** — exhaustion must fast-fail, not queue every request 30s into a
  retry storm.
- **sync** for Celery tasks: **psycopg (v3)**, not legacy psycopg2; derived from the same
  URL, larger pool.
- `async_sessionmaker(expire_on_commit=False)`.
- **Failure mapping**: pool `TimeoutError` and mid-request disconnects
  (`OperationalError`/`InterfaceError` — `pool_pre_ping` only guards checkout) → 503 with
  stable code `db_unavailable` + a `pool_exhausted` metric. `/readyz` uses its own ~1s
  DB-ping timeout so exhaustion pulls the pod from rotation instead of hanging (01).
- **Server-side timeouts on by default**: `statement_timeout` (constant, default 10s —
  must sit under the validated hierarchy with `REQUEST_TIMEOUT_S=15 < SHUTDOWN_GRACE_S=20`)
  and `lock_timeout` (5s) via connect args. A runaway query must die in the DB, not hold a
  pool slot forever. Settings validation enforces
  `statement_timeout ≤ REQUEST_TIMEOUT_S < SHUTDOWN_GRACE_S` (01). Tasks/scripts may
  raise per-session.
- **Pool math documented, not implied**: total connections =
  `pool_size × uvicorn workers × replicas` (+ worker sync pool) and must fit PG
  `max_connections`. README shows the formula.
- **pgbouncer is a first-class mode, not a footnote**: `DB_POOLER=none|pgbouncer`.
  `pgbouncer` flips `statement_cache_size=0`, `prepared_statement_cache_size=0`, and
  `prepared_statement_name_func=uuid` per SQLAlchemy's documented recipe — otherwise the
  first scale-out hits `DuplicatePreparedStatementError`. Note pgbouncer ≥ 1.21
  `max_prepared_statements` as the alternative.

## get_db() and the commit ordering (the data-loss rule)

FastAPI ≥ 0.106 runs yield-dependency teardown **after the response is sent**. Commit in
teardown therefore means: client gets 200, then commit runs — a serialization error,
statement timeout, or failover at that instant silently loses "committed" data. So:

- `get_db()` yields a request-scoped session and **parks it on `request.state`**.
- The registrar's endpoint wrapper (01) calls `session.commit()` after the service method
  returns and **before the response is built** — commit failures surface as 500s.
  Handlers never call commit.
- Teardown does rollback-if-open + close only, catching `BaseException` (client-disconnect
  `CancelledError` is not `Exception`) so disconnects roll back instead of leaking.

## Base & mixins

`database/base.py`:
- `Base(DeclarativeBase)`
- `UUIDMixin` — UUID pk, server-side `gen_random_uuid()`
- `TimestampMixin` — `created_at` **and** `updated_at` (onupdate), tz-aware
- `SoftDeleteMixin` — `deleted_at` nullable + default query filtering helper (opt-in)
- **No active-record CRUD base.** Definable's `CRUD` opened its own sessions per call —
  footgun, dropped. Queries live in services, always on the request/task-scoped session.

## Models & alembic

- One file per domain in `src/models/*_model.py`.
- **Autodiscovery**: `models/__init__.py` walks the package with `pkgutil` and imports
  every module — autogenerate can never silently miss a model.
- Async alembic (`async_engine_from_config` + `run_sync`), URL from env only.
- Migrations run explicitly (`sg db migrate` / CI step), not on container boot; compose
  dev profile may opt in.
- **Zero-downtime policy (expand/contract)**: migrations must be compatible with the
  previous app version — add-then-backfill-then-constrain; destructive steps ship one
  release after the code stops using them. Documented in README's deploy section.
- **Multiple-heads guard**: CI fails on more than one head or model↔migration drift.

## Framework schema

The `singularity.*` schema (script runs, future framework state) is owned entirely by
[08-scripts.md](08-scripts.md). `Base.metadata` and alembic cover `public` only.

## SQL observability hook

SQLAlchemy engine events (`before/after_cursor_execute`) feed query spans into the journey
tracer (06) — statement (params redacted), duration, rowcount. Toggle via settings.

## Settings (owned by this plan)

`DATABASE_URL`, `DB_POOLER`.
