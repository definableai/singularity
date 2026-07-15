"""Celery app + @task decorator (04).

Reliability defaults are the point: autoretry+backoff+jitter, acks_late +
reject_on_worker_lost (tasks MUST be idempotent — redelivery is the contract),
visibility_timeout ≥ time limit (Redis transport redelivers still-RUNNING tasks past
it — concurrent duplicates, not crash recovery), dead-letter on retry exhaustion.
"""

import time

from celery import Celery, signals

from src.config.settings import settings

# Constants (01's earning rule)
TASK_TIME_LIMIT_S = 600
TASK_SOFT_TIME_LIMIT_S = 540
RESULT_EXPIRES_S = 24 * 3600
VISIBILITY_TIMEOUT_S = TASK_TIME_LIMIT_S + 300  # margin over the longest task
MAX_RETRIES = 3
DEAD_LIST = "singularity:dead"

celery_app = Celery(
    "singularity",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_time_limit=TASK_TIME_LIMIT_S,
    task_soft_time_limit=TASK_SOFT_TIME_LIMIT_S,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    result_expires=RESULT_EXPIRES_S,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_S},
    redbeat_redis_url=settings.redis_url,
    beat_scheduler="redbeat.RedBeatScheduler",
)

_pending_submits_key = "singularity_pending_submits"  # set on the journey by get_db (02)


def task(name: str, schedule=None, **options):
    """@task(name="emails.send_digest", schedule=crontab(hour=6))

    ctx injected as first arg; retries by default (opting OUT is the explicit act);
    schedule declared inline so name and beat entry can't drift apart.
    """
    options.setdefault("autoretry_for", (Exception,))
    options.setdefault("retry_backoff", True)  # exponential
    options.setdefault("retry_backoff_max", 600)
    options.setdefault("retry_jitter", True)
    options.setdefault("max_retries", MAX_RETRIES)
    options.setdefault("ignore_result", True)

    def decorator(fn):
        def run(*args, **kwargs):
            from src.core.context import Context
            from src.tracing import journey as jmod

            ctx = Context(settings)
            headers = getattr(run_task.request, "headers", None) or {}
            parent = headers.get("trace_id", "")
            j = jmod.start("TASK", f"task:{name}", request_id=parent)
            t0 = time.perf_counter()
            from src.common.logger import log

            log.info(f"task start: {name}")
            try:
                result = fn(ctx, *args, **kwargs)
                log.info(f"task done: {name}")
                return result
            except Exception as e:
                j.error = f"{type(e).__name__}: {e}"
                j.add_step("exception", type(e).__name__, message=str(e)[:500])
                log.error(f"task failed: {name}: {e!r}")
                raise
            finally:
                from src.obs import get_pipeline, red

                duration_ms = (time.perf_counter() - t0) * 1000
                red.observe("task", name, "TASK", "5xx" if j.error else "2xx", duration_ms)
                if (p := get_pipeline()) is not None:
                    payload = jmod.finish(j, 0 if j.error is None else 1)
                    payload["extra"]["duration_ms"] = duration_ms
                    if parent:
                        payload["extra"]["parent_trace_id"] = parent
                    p.enqueue("journey", payload)

        run.__name__ = fn.__name__
        run_task = celery_app.task(name=name, **options)(run)

        if schedule is not None:
            existing = getattr(celery_app.conf, "beat_schedule", None) or {}
            celery_app.conf.beat_schedule = {
                **existing,
                name: {"task": name, "schedule": schedule},
            }
        return run_task

    return decorator


def submit_task(name: str, *args, immediate: bool = False, **kwargs):
    """One safe submit path. Inside a request with an open DB transaction, the submit
    defers to post-commit (rollback → never sent) unless immediate=True. At-most-once:
    a post-commit submit that fails (broker down) is an ERROR obs event, not a retry."""
    from src.tracing.journey import current, trace_id_var

    headers = {"trace_id": trace_id_var.get("")}

    def send():
        try:
            celery_app.send_task(name, args=args, kwargs=kwargs, headers=headers)
        except Exception as e:
            from src.common.logger import log

            log.error(
                f"post-commit task submit FAILED: {name} args_digest={hash(str(args))}: {e!r}"
            )
            raise

    if not immediate and (j := current()) is not None:
        pending = getattr(j, _pending_submits_key, None)
        if pending is not None:  # request context with an active session → defer
            pending.append(send)
            j.add_step("task_submit", name, deferred=True)
            return
    if (j := current()) is not None:
        j.add_step("task_submit", name, deferred=False)
    send()


@signals.task_failure.connect
def _dead_letter(sender=None, task_id=None, exception=None, args=None, kwargs=None, **_):
    """Retries exhausted → dead list (inspectable, re-submittable) + ERROR obs event.
    The dead list is an unbounded OOM fuse — its depth is reported (09 overview)."""
    import orjson
    import redis as redis_sync

    from src.common.logger import log

    log.error(f"task dead: {getattr(sender, 'name', '?')} id={task_id}: {exception!r}")
    try:
        r = redis_sync.from_url(settings.redis_url)
        r.rpush(
            DEAD_LIST,
            orjson.dumps(
                {
                    "name": getattr(sender, "name", "?"),
                    "task_id": task_id,
                    "args": repr(args)[:500],
                    "kwargs": repr(kwargs)[:500],
                    "error": repr(exception)[:500],
                    "ts": time.time(),
                }
            ),
        )
    except Exception as e:
        log.error(f"dead-letter push failed: {e!r}")


@signals.worker_process_init.connect
def _init_obs(**_):
    import src.obs as obs

    obs.init(settings)
