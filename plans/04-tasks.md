# 04 — Background tasks (Celery)

Status: building — shipped: celery app (acks_late + reject_on_worker_lost,
visibility_timeout > time limit, JSON/UTC, RedBeat config, result_expires), @task
(ctx first arg, autoretry+backoff+jitter, inline schedule=, journey per execution with
parent trace link, start/done/fail logs), submit_task (defer-to-post-commit via journey
+ registrar flush, immediate=True, trace headers), dead-letter redis list + ERROR event,
worker entrypoint (threads pool, --beat mode, module autodiscovery, obs init signal),
example task. Verified against a real worker. RED task rollups, sg tasks dead CLI, and queue-routing example all shipped. Complete for v1.

## Celery app

`tasks/celery.py` — factory (kept from definable's `common/q.py`, cleaned):
- Redis broker + backend, JSON serialization, UTC, `task_time_limit` (constant),
  `worker_prefetch_multiplier=1`, `task_track_started=True`.
- **`broker_transport_options.visibility_timeout` ≥ max(task_time_limit) + margin**,
  stated right next to the acks_late choice: the Redis transport's default is 1h, and any
  task outstanding longer is *redelivered while the original still runs* — concurrent
  duplicate execution, not re-run-after-crash.
- **RedBeat** scheduler — schedules persisted in Redis, survive restarts.
- `beat_schedule` assembled from `@task(schedule=...)` declarations, not a hand-synced
  dict.
- Compose Redis runs **`appendonly yes`**: with acks_late the queue IS the durability
  story — a Redis restart without AOF vaporizes every queued task (07).

## @task decorator

```python
@task(name="emails.send_digest", schedule=crontab(hour=6), ignore_result=True)
def send_digest(ctx: Context): ...
```

- **`ctx` is injected as the first argument** (worker-appropriate construction) — the rule
  is uniform across the template: everything you write receives ctx.
- Wraps `celery_app.task` with: structured start/finish/fail logging into the obs
  pipeline (05), **task RED counters** (this wrapper owns task rate/errors/duration
  rollups, as 01's core layer owns request RED), trace-id propagation (06), and optional
  `schedule=` which registers the beat entry — name and schedule can't drift apart.
- **One safe submit path, every spelling**: the wrapper overrides `apply_async`, so
  `.delay()`, `.apply_async()`, and `submit_task("name", ...)` (by-name form) all get
  trace headers and submit-after-commit semantics. No bypass exists to forget.

## Reliability semantics (the production part)

- **Retries by default**: `autoretry_for=(Exception,)`, exponential backoff with jitter,
  `max_retries=3` — overridable per task. Opting *out* is the explicit act.
- **`acks_late=True` + `task_reject_on_worker_lost=True`**: a task that dies with its
  worker is redelivered, not vanished. Consequence in the scaffold docstring: **tasks
  must be idempotent** — the generator template says so and shows the pattern (natural
  keys / upsert / dedup guard).
- **Dead-letter**: a task that exhausts retries lands in a `dead` queue and emits an obs
  event. The dead queue gets an **obs depth gauge** — it is an unbounded OOM fuse
  otherwise. Inspect/re-submit via `sg tasks dead ls|retry` (07).
- **Result hygiene**: `result_expires` set (24h); `ignore_result=True` is the scaffold
  default.
- **Queues**: named queues + per-task routing from day 1 (`@task(queue="heavy")`); compose
  shows a second worker consuming it. Priorities: not v1.
- **Framework beat task shipped**: `obs.maintain_store` (daily) — creates tomorrow's
  `records` partition, drops those past `OBS_RETENTION_DAYS` (09). Boot's ensure_schema
  covers today+tomorrow, so a dead beat never gaps ingestion.

## Submit-after-commit

`submit_task` (or `.delay`) inside a request while the DB session has uncommitted work is
the classic race: worker reads a row that doesn't exist yet. Semantics:

- A submit during an active request session is **deferred to post-commit**; rollback →
  task never sent. `submit_task(..., immediate=True)` opts out.
- Post-commit submits run **shielded, synchronously in the commit frame** — a request
  cancelled between commit and hook must not silently drop the task while the DB change
  persists.
- A post-commit submit that still fails (broker down) is an **ERROR obs event** carrying
  task name + args digest + request_id — loud, greppable, re-submittable.
- README names the semantics honestly: **at-most-once submission**; the transactional
  outbox is the graduation path if a project needs exactly-once.

## Worker entrypoint

`tasks/worker.py`:
- **`--pool=threads` is the default** — same IO concurrency as gevent with zero
  monkey-patching; the whole "parse `--pool` pre-import so `gevent.monkey.patch_all()`
  runs first" boot dance is deleted (Celery's asyncio story is not coming). prefork
  documented for CPU-bound workloads. Works with the sync engine unchanged (02).
- Task modules autodiscovered via `pkgutil` walk (recursive).
- Provider/obs init on both `worker_process_init` and `worker_ready` signals; **bounded
  obs flush on `worker_shutdown`** — same shutdown parity as the API (05).
- **Beat is its own process**: compose runs `beat` as a dedicated single-replica service;
  workers always `--no-beat`.

## Obs & tracing in the worker

Non-negotiable (definable's gap): the obs capture sink (05) and journey tracer (06)
initialize in the worker exactly as in the API. A task execution is a journey of its own,
linked to the originating request's trace id when submitted from an endpoint.

## Settings (owned by this plan)

None — broker/backend derive from `REDIS_URL` (01); task limits and retry defaults are
constants per 01's earning rule.
