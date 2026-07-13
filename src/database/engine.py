"""Engines + get_db (02).

Commit ordering rule: FastAPI ≥0.106 runs yield-dependency teardown AFTER the response
is sent — commit in teardown means silent "200 but rolled back" data loss. So get_db
parks the session on request.state; the registrar's endpoint wrapper commits BEFORE the
response is built. Teardown only rolls back if still open, catching BaseException
(client-disconnect CancelledError is not Exception).
"""

import time
from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from src.config.settings import STATEMENT_TIMEOUT_S, settings

# Constants (01's earning rule)
POOL_SIZE = 5
POOL_TIMEOUT_S = 5  # exhaustion must fast-fail to 503, not queue 30s into a retry storm
LOCK_TIMEOUT_S = 5
SQL_STEP_STATEMENT_CHARS = 200

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _session_factory
    if _engine is None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        connect_args: dict = {
            "server_settings": {
                "statement_timeout": str(STATEMENT_TIMEOUT_S * 1000),
                "lock_timeout": str(LOCK_TIMEOUT_S * 1000),
            }
        }
        if settings.db_pooler == "pgbouncer":
            # SQLAlchemy's documented recipe: prepared statements break under
            # transaction-mode pgbouncer
            connect_args["statement_cache_size"] = 0
            connect_args["prepared_statement_cache_size"] = 0
        _engine = create_async_engine(
            settings.database_url,
            pool_size=POOL_SIZE,
            pool_timeout=POOL_TIMEOUT_S,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        _install_sql_steps(_engine)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    session = session_factory()()
    request.state.db_session = session
    from src.tracing import journey

    if (j := journey.current()) is not None:
        # submit-after-commit (04): task submits during this request defer here; the
        # registrar wrapper flushes the list right after commit, rollback discards it
        setattr(j, "singularity_pending_submits", [])
    try:
        yield session
    except BaseException:
        # CancelledError (client disconnect / timeout) is not Exception — must roll back.
        raise
    finally:
        try:
            if session.in_transaction():
                await session.rollback()
        finally:
            await session.close()


_sync_engine = None


def get_sync_session():
    """DB access for Celery tasks (threads pool → sync engine, psycopg v3):

        with get_sync_session() as session:
            session.execute(...)
            session.commit()
    """
    global _sync_engine
    if _sync_engine is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        _sync_engine = create_engine(
            settings.database_url.replace("+asyncpg", "+psycopg"),
            pool_size=POOL_SIZE,
            pool_timeout=POOL_TIMEOUT_S,
            pool_pre_ping=True,
        )
        _install_sql_steps_sync(_sync_engine)
        get_sync_session._factory = sessionmaker(_sync_engine)
    return get_sync_session._factory()


def _install_sql_steps_sync(engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._journey_t0 = time.perf_counter()

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        from src.tracing import journey

        if (j := journey.current()) is not None:
            j.add_step(
                "sql",
                statement[:SQL_STEP_STATEMENT_CHARS],
                duration_ms=(time.perf_counter() - getattr(context, "_journey_t0", time.perf_counter())) * 1000,
                rows=cursor.rowcount,
            )


def _install_sql_steps(engine: AsyncEngine) -> None:
    # Query spans into the journey (06): statement (params never recorded), duration, rows.
    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        context._journey_t0 = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        from src.tracing import journey

        if (j := journey.current()) is not None:
            j.add_step(
                "sql",
                statement[:SQL_STEP_STATEMENT_CHARS],
                duration_ms=(time.perf_counter() - getattr(context, "_journey_t0", time.perf_counter())) * 1000,
                rows=cursor.rowcount,
            )
