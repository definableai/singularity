"""Async alembic env (02). URL from DATABASE_URL only; models autodiscovered."""

import asyncio
import os

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

import src.models  # noqa: F401 — autodiscovery imports every model module
from src.database.base import Base

config = context.config
target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to):
    # alembic owns `public` only; the `singularity` schema is the framework's (08)
    return getattr(obj, "schema", None) is None


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, include_object=_include_object)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    from sqlalchemy import text

    # alembic owns `public` only. Without this, a DB user named like the framework
    # schema (default search_path is "$user", public) makes reflection see the
    # singularity.* tables as schema-less and autogenerate tries to DROP them.
    connection.execute(text("SET search_path TO public"))
    connection.dialect.default_schema_name = "public"
    context.configure(connection=connection, target_metadata=target_metadata, include_object=_include_object)
    with context.begin_transaction():
        context.run_migrations()
    # the SET search_path above autobegan a transaction, which makes alembic's
    # begin_transaction() a nested no-op — commit explicitly or it all rolls back
    connection.commit()


async def run_migrations_online() -> None:
    engine = async_engine_from_config({"sqlalchemy.url": _url()}, prefix="sqlalchemy.")
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
