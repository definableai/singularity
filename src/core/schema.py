"""The `singularity.*` framework schema (08 owns creation; 09 defines the obs shapes).

ensure_schema() runs at app boot AND CLI start, wrapped in an advisory lock because
concurrent CREATE ... IF NOT EXISTS is NOT race-safe in PG (duplicate-key on
pg_namespace under multi-replica boot).
"""

from datetime import date, timedelta

from sqlalchemy import text

SCHEMA_VERSION = 1
ENSURE_LOCK_KEY = 0x5147_0001  # constant advisory-lock key for schema DDL

_DDL = """
CREATE SCHEMA IF NOT EXISTS singularity;

CREATE TABLE IF NOT EXISTS singularity.meta (
  version int NOT NULL
);

CREATE TABLE IF NOT EXISTS singularity.script_run (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  kind text NOT NULL,
  checksum text NOT NULL,
  status text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  duration_ms int,
  error text,
  output jsonb,
  triggered_by text NOT NULL,
  forced bool NOT NULL DEFAULT false,
  host text, pid int,
  trace_id text
);
CREATE INDEX IF NOT EXISTS script_run_name_idx ON singularity.script_run (name, started_at DESC);

CREATE TABLE IF NOT EXISTS singularity.records (
  ts timestamptz NOT NULL,
  kind text NOT NULL,
  level text,
  trace_id text, request_id text, principal_id text,
  name text,
  status text, duration_ms int,
  fingerprint text,
  message text,
  attributes jsonb
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS records_ts_brin ON singularity.records USING brin (ts);
CREATE INDEX IF NOT EXISTS records_trace_idx ON singularity.records (trace_id);
CREATE INDEX IF NOT EXISTS records_fp_idx ON singularity.records (fingerprint)
  WHERE fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS singularity.issue (
  fingerprint text PRIMARY KEY,
  title text NOT NULL,
  state text NOT NULL DEFAULT 'unresolved',
  first_seen timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now(),
  event_count bigint NOT NULL DEFAULT 0,
  user_count bigint NOT NULL DEFAULT 0,
  sample_trace_ids text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS singularity.view (
  id text PRIMARY KEY,
  name text NOT NULL,
  spec jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS singularity.dashboard (
  id text PRIMARY KEY,
  name text NOT NULL,
  tiles jsonb NOT NULL DEFAULT '[]',
  created_at timestamptz NOT NULL DEFAULT now()
);
"""


def _partition_ddl(day: date) -> str:
    name = f"records_{day:%Y%m%d}"
    return (
        f"CREATE TABLE IF NOT EXISTS singularity.{name} PARTITION OF singularity.records "
        f"FOR VALUES FROM ('{day}') TO ('{day + timedelta(days=1)}')"
    )


async def ensure_schema() -> None:
    """Idempotent, race-safe. Creates today's and tomorrow's records partitions so a
    scheduler-less dev setup never gaps ingestion at midnight."""
    from src.database.engine import get_engine

    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ENSURE_LOCK_KEY})
        try:
            for stmt in _DDL.split(";"):
                if stmt.strip():
                    await conn.execute(text(stmt))
            today = date.today()
            await conn.execute(text(_partition_ddl(today)))
            await conn.execute(text(_partition_ddl(today + timedelta(days=1))))
            has_version = (
                await conn.execute(text("SELECT version FROM singularity.meta LIMIT 1"))
            ).scalar()
            if has_version is None:
                await conn.execute(
                    text("INSERT INTO singularity.meta (version) VALUES (:v)"),
                    {"v": SCHEMA_VERSION},
                )
            await conn.commit()
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ENSURE_LOCK_KEY})
