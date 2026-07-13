# Singularity

FastAPI boilerplate: the fastest path from `git clone` to a production-grade backend —
with a built-in observability dashboard (Sentry + mini-BI) and a line-level flight
recorder for every request.

**You write four kinds of things** — a **Service** (handles requests), a **Model**
(holds data), a **Task** (background work), a **Script** (operational change). Wherever
your code needs the framework, it's one object: `ctx`. Everything all four do is
recorded automatically; one CLI (`sg`) drives it all.

## Quickstart

```bash
git clone <this repo> myapp && cd myapp
uv sync
cp .env.template .env            # set ENVIRONMENT=dev, DATABASE_URL, REDIS_URL
docker compose up -d             # postgres + redis
uv run sg doctor                 # every check green?
uv run sg db migrate
uv run uvicorn src.app:app --reload
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) for the API and
[http://localhost:8000/__obs/](http://localhost:8000/__obs/) for **Observatory** —
traces (with per-line variable state), logs, issues, live stream. No setup; it's
recording already.

## Your first service

```bash
uv run sg g service orders      # scaffolds src/services/orders/service.py
```

```python
class OrdersService:
    http_exposed = ["post=create", "get=list", "get=detail/{id}"]

    def __init__(self, ctx: Context): ...

    async def post_create(self, data: OrderCreate, session=Depends(get_db)) -> OrderOut:
        ...  # no commit needed — the framework commits BEFORE the response is sent
```

Folder path = URL path (`services/orders/` → `/api/v1/orders`). Boot fails loudly on any
entry↔method mismatch. Every request is recorded — open `/__obs`, click the trace, see
the call tree with arguments, return values, and per-line variable state. No decorators,
no spans, nothing to remember.

## Auth in two lines

```bash
# .env
AUTH_PROVIDERS=["jwt"]
JWT_SECRET=your-secret          # or JWT_JWKS_URL=https://issuer/.well-known/jwks.json
```

```python
from src.auth.deps import Auth
from src.auth.protocol import Principal

class OrdersService:
    auth = Auth()                                    # whole service behind auth, or:
    async def get_list(self, user: Principal = Depends(Auth())): ...
```

Bring your own vendor by copying `src/auth/providers/stytch_example.py` and listing its
dotted path in `AUTH_PROVIDERS`. In tests: `client.as_user("usr_1")`. In dev:
`AUTH_DEV_PRINCIPAL=usr_dev` (boot error outside dev).

## The CLI

| command | does |
|---|---|
| `sg doctor` | why won't it boot — every check prints pass/fail + the fix |
| `sg g service/model/task/script <name>` | scaffolding |
| `sg db migrate` / `makemigration "msg"` | alembic |
| `sg db grant-readonly` | SQL for the data-views read-only role |
| `sg script run --pending` | deploy step: ordered once-scripts + changed repeatables |
| `sg trace <id> [--lines]` | terminal journey viewer |
| `sg tasks dead ls\|retry` | dead-letter queue |
| `sg config sync [--check]` | regenerate `.env.template` from Settings (CI gate) |
| `sg api snapshot [--check]` | OpenAPI snapshot gate (CI) |
| `sg errors export` | stable error-code catalog for frontends |

## Deploy notes (the sharp edges, documented)

- **Deploy contract**: `sg db migrate && sg script run --pending`, then roll pods.
  Migrations/scripts never run from container entrypoints.
- **k8s grace math**: `terminationGracePeriodSeconds ≥ preStop + SHUTDOWN_GRACE_S +
  OBS_FLUSH_TIMEOUT_S + slack` (default 20 + 5 → use ≥ 35).
- **Pool math**: PG connections = `pool_size (5) × uvicorn workers × replicas` + worker
  pool — keep under `max_connections`. At scale, set `DB_POOLER=pgbouncer`.
- **Task submits are at-most-once**: a submit inside a request defers to post-commit
  (rollback = never sent). Need exactly-once? That's a transactional outbox — build it
  when a project demands it.
- **Tasks must be idempotent**: `acks_late` redelivers tasks whose worker died.
- **WebSockets are single-replica**: the in-process manager only reaches its own
  replica's connections. Scale WS beyond one replica → add a Redis pub/sub backplane.
- **Alerting before you build anything**: prod logs go to stdout as JSON — point your
  platform's ERROR-rate alert (CloudWatch/Loki/Datadog) at them.
- **Uvicorn has no hung-worker reaper**: run 1–2 workers per container and scale by
  replicas so container liveness restarts cover a wedged process.
- **Observatory outside dev** needs `DASHBOARD_TOKEN` (or override the `dashboard_auth`
  dependency with your own auth).

## Plans

`PLAN.md` + `plans/` are the living design docs — one file per subsystem, updated in the
same commit as the code they describe.
