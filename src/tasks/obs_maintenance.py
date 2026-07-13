"""Framework beat task (04/09): records partition create/drop.

Boot's ensure_schema covers today+tomorrow, so a dead beat never gaps ingestion; this
task extends the horizon and drops partitions past retention (constant-time DROP,
reclaims disk — never DELETE).
"""

from datetime import date, timedelta

from celery.schedules import crontab

from src.tasks.celery_app import task


@task(name="obs.maintain_store", schedule=crontab(hour=1, minute=0), max_retries=1)
def maintain_store(ctx):
    import psycopg

    from src.core.schema import _partition_ddl

    retention_days = ctx.settings.obs_retention_days
    dsn = ctx.settings.database_url.replace("+asyncpg", "")
    dropped = []
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_partition_ddl(date.today() + timedelta(days=1)))
        cur.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname='singularity' AND tablename ~ '^records_[0-9]{8}$'"
        )
        cutoff = f"records_{date.today() - timedelta(days=retention_days):%Y%m%d}"
        for (name,) in cur.fetchall():
            if name < cutoff:
                cur.execute(f"DROP TABLE singularity.{name}")
                dropped.append(name)
    return {"dropped": dropped}
