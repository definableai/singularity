# Singularity — Master Plan

FastAPI boilerplate: fastest path from `git clone` to a production-grade backend.
Convention layer + batteries on top of FastAPI (same relation FastAPI has to Starlette).
Derived from definable.backend's proven patterns; its warts fixed.

**The pitch, four nouns:** You write four kinds of things — a **Service** (handles
requests), a **Model** (holds data), a **Task** (background work), a **Script**
(operational change). Wherever your code needs the framework, it is exactly one object:
**ctx** (services and scripts take it directly; models and tasks are plain declarations
the framework wires up). Everything all four do is recorded automatically — no
decorators, no trace calls; one CLI (`sg`) drives it all. Observability is a guarantee,
not a fifth concept.

This file is the index. Each subsystem has its own plan file — a plan file is the source
of truth for its subsystem's design.

## Plan files

| # | Plan | Covers | Status |
|---|------|--------|--------|
| 01 | [plans/01-core.md](plans/01-core.md) | app factory, runtime (uvicorn/orjson), settings discipline, registrar (`http_exposed`), Context, core ASGI layer, cancellation contract, errors, cache/locks, ws | draft |
| 02 | [plans/02-database.md](plans/02-database.md) | engines, commit-before-response, mixins, alembic autodiscovery, pgbouncer mode | draft |
| 03 | [plans/03-auth.md](plans/03-auth.md) | pluggable AuthProvider; hardened jwt (alg pinning, JWKS ops), api-key, Stytch example | draft |
| 04 | [plans/04-tasks.md](plans/04-tasks.md) | Celery (threads pool), RedBeat, @task(ctx), retries/DLQ, visibility_timeout, submit-after-commit | draft |
| 05 | [plans/05-observability.md](plans/05-observability.md) | **custom obs system** — ≤5µs capture, one pipeline, stdout prod default, journey store, pre-aggregated RED, audit | draft |
| 06 | [plans/06-tracing.md](plans/06-tracing.md) | **request-journey tracing** — sys.monitoring call trees + line-level state, on_error prod mode, N+1 detector, replay | draft |
| 07 | [plans/07-dx.md](plans/07-dx.md) | `sg` CLI registry, doctor, config sync, api snapshot gate, tests harness, CI, docker/compose | draft |
| 08 | [plans/08-scripts.md](plans/08-scripts.md) | **scripts subsystem** — once/repeatable/manual, forward-only, `singularity` PG schema | draft |
| 09 | [plans/09-dashboard.md](plans/09-dashboard.md) | **Observatory** — in-app dashboard: PG store, logs/traces/issues (Sentry half), data views + SQL→UI (Power BI half), live tail | draft |

## Locked decisions

- **Template-first**: clone the repo, own the code. Extract installable `singularity`
  package once the API is proven across 2–3 projects.
- **Routing**: definable's `http_exposed = ["post=create", ...]` nomenclature, made
  STRICT (boot fails on service import error or entry↔method mismatch; warns on orphan
  `{verb}_{path}` methods). Folder path → URL path.
- **Auth**: bring-your-own via `AuthProvider` protocol. Ships hardened JWT (secret/JWKS)
  + API-key; Stytch adapter as the BYO example. No RBAC baked in.
- **Observability is first-party**: no third-party obs provider dependency. Our own
  capture pipeline (05) and request-journey tracing (06). Sentry/OTel are optional
  add-ons, not the backbone.
- **Python ≥ 3.12**: the flight recorder (06) is built on `sys.monitoring` (PEP 669).
- **PostgreSQL is a hard dependency**: the framework owns a dedicated `singularity` PG
  schema (08), separate from the app's alembic-managed `public` schema.
- **Runtime**: `uvicorn --workers` (no gunicorn), uvloop + httptools, orjson everywhere.
- **Settings earning rule**: an env var exists only if it differs between deployments;
  everything else is a constant in source (01).

## Glossary (the concept budget)

The complete list of framework concepts a developer must learn. **Maintenance rule:
adding a user-facing concept requires deleting or merging one.**

| concept | one line |
|---|---|
| Service | class with `http_exposed`; folder path = URL path |
| Model | declarative table in `src/models/`, autodiscovered |
| Task | `@task` function; ctx first arg; beat schedule declared inline |
| Script | BaseScript in `src/scripts/`; once / repeatable / manual, DB-recorded |
| ctx | the one framework object: settings, cache, http, ws, lock |
| log | the one telemetry surface: `log.info/.../metric/audit` |
| Principal / AuthProvider | who's calling / how they're verified |
| journey | the automatic recording of one request / task / script run, flowing as events through one pipeline into the PG store |
| Observatory | the built-in dashboard at `/__obs`: logs, traces, issues, data views |
| view spec | saved SQL + roles + chart encoding; tiles re-render from it |
| sg | the CLI: generate, migrate, run scripts, trace, doctor |
| Settings | pydantic-settings class; generates `.env.template`; earning rule |

## Project layout

```
singularity/
├── PLAN.md, plans/           # this plan tree — maintained, versioned
├── pyproject.toml            # uv; server/worker/sg entrypoints
├── compose.yaml              # postgres, redis, api, worker (--no-beat), beat (single)
├── Dockerfile
├── alembic/
├── .env.template             # generated by `sg config sync`; never commit .env
├── src/
│   ├── app.py                # create_app() + dev/prod runner
│   ├── core/                 # framework: registrar, context, core ASGI layer, errors, loader
│   ├── config/settings.py
│   ├── database/             # engine.py, base.py
│   ├── auth/                 # protocol, providers/, deps
│   ├── middlewares/          # ships EMPTY — escape hatch (pure ASGI)
│   ├── common/               # logger, cache, http, websocket
│   ├── obs/                  # 05: capture pipeline (sink, buffer, transports, store)
│   ├── tracing/              # 06: journey tracer, engine, viewer
│   ├── tasks/                # celery.py, worker.py
│   ├── models/
│   ├── services/healthz/
│   ├── scripts/              # BaseScript files ONLY
│   └── cli/                  # sg CLI core
└── tests/
```

## Definable warts fixed (summary — details in subsystem plans)

silent service-import swallow → boot fails loud · RBAC no-op → real pluggable auth ·
Sentry API-only → obs pipeline in api AND worker · no request correlation → request-id +
trace everywhere · hand-maintained models/__init__ → pkgutil autodiscovery · beat on every
worker → dedicated beat service · self-opening-session CRUD base → dropped · created_at
only → +updated_at, +SoftDeleteMixin · in-memory rate limiter → dropped (gateway job) ·
committed secrets → env-only · teardown-commit data loss → commit-before-response ·
gevent monkey-patch dance → threads pool.

## Build order

1. **Core boot** (01): settings, registrar, Context, core ASGI layer, `/livez` —
   `uv run server --dev` works (`/readyz` completes at step 4 when DB/Redis clients exist)
2. **Obs foundation** (05): logger + capture sink + flusher + jsonl/stdout transports —
   logs recorded from day 1
3. **Tracing core** (06): trace context, T0 boundaries, dev viewer skeleton
4. **DB** (02): engines, commit-before-response, mixins, get_db, alembic, first
   migration; `/readyz` complete
5. **Scripts + framework schema** (08): ensure_schema, BaseScript, runner + advisory
   lock; then 01's ctx.lock and ctx.cache land (reusing 08's lock machinery + Redis client)
6. **Auth** (03): protocol, jwt + api_key, Auth() dep, Stytch example
7. **Tasks** (04): celery, RedBeat, threads worker; obs + trace propagation into worker
8. **Obs/tracing deepen** (05/06): PG store + partitions (09 slice 1), sys.monitoring
   engine (T1/T2), budgets, on_error mode, N+1 + replay
9. **Observatory** (09): dashboard shell + auth + self-exclusion, logs/traces views,
   SSE live tail, issues, data views + guarded executor, dashboards/tiles
10. **DX** (07): `sg` CLI full registry, doctor, config sync, api snapshot, tests
    harness, compose, CI, README

## Maintenance rules

- Any design change lands in the matching plan file in the same commit as the code.
- Plan files carry a `Status:` line (draft → building → shipped) per section.
- Every plan file ends with a `## Settings` block naming exactly the env vars it owns;
  a setting named only in prose does not exist.
- Adding a user-facing concept to the glossary requires deleting or merging one.
