"""Example task — the shape to copy. Tasks are idempotent: acks_late means a task that
dies with its worker is REDELIVERED (that's the durability contract), so running twice
must be safe — use natural keys / upserts / dedup guards."""

from src.tasks.celery_app import task


@task(name="example.ping")
def ping(ctx, marker: str = "pong"):
    import redis as redis_sync

    r = redis_sync.from_url(ctx.settings.redis_url)
    r.set("singularity:example:ping", marker, ex=60)
    return {"marker": marker}
