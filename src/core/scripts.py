"""Scripts subsystem (08): once / repeatable / manual, recorded in singularity.script_run.

Forward-only — no rollback(); to undo a script, write the next script.
"""

import hashlib
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import orjson
from sqlalchemy import text

from src.core.context import Context

KINDS = ("once", "repeatable", "manual")


class ScriptError(Exception):
    pass


class BaseScript:
    kind = "manual"
    description = ""

    async def run(self, ctx: Context) -> dict | None:  # return value stored as run output
        raise NotImplementedError


@dataclass
class Discovered:
    name: str
    kind: str
    checksum: str
    cls: type[BaseScript]
    order: int  # numeric filename prefix for `once` ordering; -1 otherwise


def discover(package_dir: str = "src/scripts") -> list[Discovered]:
    """Strict-loudness: bad import = error, not skip. src/scripts holds ONLY scripts."""
    import importlib

    found = []
    for path in sorted(Path(package_dir).glob("*.py")):
        if path.name.startswith("_"):
            continue
        module = importlib.import_module(f"src.scripts.{path.stem}")
        cls = getattr(module, "Script", None)
        if cls is None or not issubclass(cls, BaseScript):
            raise ScriptError(f"{path}: no Script(BaseScript) class")
        if cls.kind not in KINDS:
            raise ScriptError(f"{path}: kind must be one of {KINDS}, got {cls.kind!r}")
        prefix = path.stem.split("_")[0]
        order = int(prefix) if prefix.isdigit() else -1
        if cls.kind == "once" and order < 0:
            raise ScriptError(f"{path}: `once` scripts need a numeric prefix (0001_...)")
        found.append(
            Discovered(
                path.stem, cls.kind, hashlib.sha256(path.read_bytes()).hexdigest(), cls, order
            )
        )
    return found


async def _latest_success(conn, name: str):
    return (
        await conn.execute(
            text(
                "SELECT checksum FROM singularity.script_run "
                "WHERE name=:n AND status='success' ORDER BY started_at DESC LIMIT 1"
            ),
            {"n": name},
        )
    ).first()


async def check_drift(scripts: list[Discovered]) -> None:
    """A `once` script edited after it ran is a lie — hard error at boot (08)."""
    from src.database.engine import get_engine

    async with get_engine().connect() as conn:
        for s in scripts:
            if s.kind != "once":
                continue
            row = await _latest_success(conn, s.name)
            if row is not None and row[0] != s.checksum:
                raise ScriptError(f"{s.name} was edited after it ran (checksum drift)")


async def pending(scripts: list[Discovered]) -> list[Discovered]:
    from src.database.engine import get_engine

    out = []
    async with get_engine().connect() as conn:
        for s in scripts:
            if s.kind == "manual":
                continue
            row = await _latest_success(conn, s.name)
            if s.kind == "once" and row is None:
                out.append(s)
            elif s.kind == "repeatable" and (row is None or row[0] != s.checksum):
                out.append(s)
    # once scripts in filename-number order, then repeatables
    return sorted(out, key=lambda s: (s.kind != "once", s.order, s.name))


async def run_script(s: Discovered, ctx: Context, triggered_by: str, force: bool = False) -> str:
    """Returns final status. Advisory-locked: N workers × M replicas → exactly one runs."""
    from src.common.lock import LockManager
    from src.database.engine import get_engine
    from src.tracing import journey as jmod

    engine = get_engine()
    async with LockManager()(f"script:{s.name}"):
        async with engine.begin() as conn:
            row = await _latest_success(conn, s.name)
            if row is not None and not force:
                if s.kind == "once" or (s.kind == "repeatable" and row[0] == s.checksum):
                    return "skipped"  # someone else ran it while we waited on the lock
            run_id = (
                await conn.execute(
                    text(
                        "INSERT INTO singularity.script_run "
                        "(name, kind, checksum, status, triggered_by, forced, host, pid, trace_id) "
                        "VALUES (:n, :k, :c, 'running', :t, :f, :h, :p, :tr) RETURNING id"
                    ),
                    {
                        "n": s.name,
                        "k": s.kind,
                        "c": s.checksum,
                        "t": triggered_by,
                        "f": force,
                        "h": socket.gethostname(),
                        "p": os.getpid(),
                        "tr": (j := jmod.start("SCRIPT", f"script:{s.name}", "")).trace_id,
                    },
                )
            ).scalar()

        t0 = time.perf_counter()
        status, error, output = "success", None, None

        async def heartbeat():
            # crash evidence: a `running` row whose heartbeat stopped is a dead runner,
            # not a slow one (08). ponytail: lock-loss abort deferred — advisory lock
            # release on connection death already prevents double-run.
            import asyncio

            while True:
                await asyncio.sleep(5)
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE singularity.script_run SET finished_at=now() WHERE id=:id AND status='running'"
                        ),
                        {"id": run_id},
                    )

        import asyncio

        hb = asyncio.create_task(heartbeat())
        try:
            output = await s.cls().run(ctx)
        except Exception as e:
            status, error = "failed", f"{type(e).__name__}: {e}"
            j.error = error
        finally:
            hb.cancel()
            duration_ms = int((time.perf_counter() - t0) * 1000)
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE singularity.script_run SET status=:s, error=:e, output=:o, "
                        "finished_at=now(), duration_ms=:d WHERE id=:id"
                    ),
                    {
                        "s": status,
                        "e": error,
                        "d": duration_ms,
                        "id": run_id,
                        "o": orjson.dumps(output).decode() if output is not None else None,
                    },
                )
            from src.obs import get_pipeline

            if (p := get_pipeline()) is not None:
                p.enqueue("journey", jmod.finish(j, 0 if status == "success" else 1))
        if status == "failed":
            raise ScriptError(f"{s.name} failed: {error}")
        return status


async def run_pending(ctx: Context, triggered_by: str) -> list[str]:
    """Deploy step / dev startup: ordered once + changed repeatables; failure stops the
    queue (later scripts may depend on earlier ones)."""
    scripts = discover()
    await check_drift(scripts)
    ran = []
    for s in await pending(scripts):
        await run_script(s, ctx, triggered_by)
        ran.append(s.name)
    return ran
