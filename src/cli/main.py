"""`sg` — the one CLI (07). Lives in src/cli/; src/scripts/ holds only BaseScript files."""

import asyncio
import os
import sys
import traceback
from pathlib import Path

import click
import orjson

ROOT = Path(__file__).resolve().parents[2]

TRACE_TAIL_FRAMES = 5  # inline: how many innermost frames to show before `-v`


def _short(path: str) -> str:
    """Repo-relative for our code, site-packages-relative for deps — kill the noise."""
    if str(ROOT) in path:
        return os.path.relpath(path, ROOT)
    if "/site-packages/" in path:
        return path.split("/site-packages/", 1)[1]
    return path


def _chain_frames(exc: BaseException) -> list[traceback.FrameSummary]:
    """Flatten the whole cause/context chain, oldest-first (print order), so the tail
    lands on the frames that actually failed — not just the wrapper that re-raised."""
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        if cur.__cause__ is not None:
            cur = cur.__cause__
        elif not cur.__suppress_context__:
            cur = cur.__context__
        else:
            break
    frames: list[traceback.FrameSummary] = []
    for e in reversed(chain):  # oldest cause → final exception
        frames.extend(traceback.extract_tb(e.__traceback__))
    return frames


def _compact_trace(exc: BaseException) -> str:
    """Inline tail: `… N hidden` + innermost frames + the exception — dimmed."""
    frames = _chain_frames(exc)
    hidden = len(frames) - TRACE_TAIL_FRAMES
    lines = []
    if hidden > 0:
        lines.append(f"… {hidden} frames hidden — sg doctor -v")
    for f in frames[-TRACE_TAIL_FRAMES:]:
        lines.append(f"{_short(f.filename)}:{f.lineno} in {f.name}")
    msg = str(exc)
    lines.append(f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__)
    return click.style("\n".join("    " + line for line in lines), fg="bright_black")


def _full_trace(exc: BaseException) -> str:
    """Plain full traceback (chain included) for the pager."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()


def _settings():
    from src.config.settings import settings

    return settings


def _run(coro):
    async def with_dispose():
        # pooled asyncpg connections and the redis client are event-loop-bound; a CLI
        # invocation may follow other loops (tests, prior commands) — shed both first
        import src.common.redis as redis_mod
        from src.database.engine import get_engine

        redis_mod._client = None
        try:
            if _settings().database_url:
                await get_engine().dispose()
            return await coro
        finally:
            try:
                await get_engine().dispose()
            except Exception:
                pass

    return asyncio.run(with_dispose())


def _ctx():
    from src.core.context import Context

    return Context(_settings())


@click.group()
def cli():
    """Singularity CLI."""


# ---------------- doctor ----------------

@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="full tracebacks, paged (less) on a TTY")
def doctor(verbose: bool):
    """Why won't it boot? Every check prints pass/fail + the fix."""
    ok = True
    failures: list[tuple[str, BaseException]] = []  # (name, exc) for -v paging

    def check(name: str, passed: bool, fix: str = "", exc: BaseException | None = None):
        nonlocal ok
        ok &= passed
        mark = click.style("✓", fg="green") if passed else click.style("✗", fg="red")
        line = f" {mark} {name}"
        if not passed:
            line += f"\n    fix: {fix}"
            if exc is not None:
                line += "\n" + _compact_trace(exc)
                failures.append((name, exc))
        click.echo(line)

    v = sys.version_info
    check(f"python {v.major}.{v.minor}", v >= (3, 12), "install Python >= 3.12 (sys.monitoring)")

    try:
        s = _settings()
        check(f"settings load (ENVIRONMENT={s.environment})", True)
    except SystemExit:
        check("settings load", False, "fix the env errors printed above (.env)")
        raise SystemExit(1)

    async def db_checks():
        from sqlalchemy import text

        from src.database.engine import get_engine

        results = {}
        if not s.database_url:
            return {"db": (False, "set DATABASE_URL in .env", None)}
        try:
            async with asyncio.timeout(3):
                async with get_engine().connect() as conn:
                    await conn.execute(text("SELECT 1"))
            results["db"] = (True, "", None)
            async with get_engine().connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM pg_tables WHERE schemaname='singularity' "
                            "AND tablename = 'records_' || to_char(now(), 'YYYYMMDD')"
                        )
                    )
                ).scalar()
            results["store partition (today)"] = (bool(rows), "boot the app once or run obs.maintain_store", None)
        except Exception as e:
            results["db"] = (False, f"start postgres (docker compose up -d) — {type(e).__name__}", e)
        return results

    for name, (passed, fix, exc) in _run(db_checks()).items():
        check(name, passed, fix, exc)

    async def redis_check():
        try:
            import src.common.redis as redis_mod

            redis_mod._client = None  # loop-bound; fresh client for this loop
            async with asyncio.timeout(2):
                await redis_mod.get_redis().ping()
            return True, None
        except Exception as e:
            return False, e

    r_ok, r_exc = asyncio.run(redis_check())
    check("redis", r_ok, "start redis (docker compose up -d)", r_exc)

    if s.database_url:
        try:
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
            heads = script.get_heads()
            check(f"alembic single head ({len(heads)})", len(heads) <= 1, "merge heads: alembic merge heads")
        except Exception as e:
            check("alembic", False, f"{e}", e)

        async def scripts_check():
            from src.core.schema import ensure_schema
            from src.core.scripts import check_drift, discover, pending

            await ensure_schema()
            found = discover()
            await check_drift(found)
            return [p.name for p in await pending(found)]

        try:
            pend = _run(scripts_check())
            check(f"scripts ({len(pend)} pending)", True)
            for name in pend:
                click.echo(f"    pending: {name}")
        except Exception as e:
            check("scripts", False, str(e), e)

    if not s.is_dev and not s.dashboard_token:
        check("dashboard token", False, "set DASHBOARD_TOKEN to enable /__obs outside dev")

    if verbose and failures:
        report = "\n\n".join(f"===== {name} =====\n{_full_trace(exc)}" for name, exc in failures)
        if sys.stdout.isatty():
            click.echo_via_pager(report)  # scroll (↑/↓), search (/), quit (q)
        else:
            click.echo(report)  # CI / pipes stay plain
    elif failures:
        click.echo(click.style("    (sg doctor -v for full tracebacks)", fg="bright_black"))

    raise SystemExit(0 if ok else 1)


# ---------------- generators ----------------

@cli.group()
def g():
    """Scaffold services, models, tasks, scripts."""


def _write_new(path: Path, content: str):
    if path.exists():
        raise click.ClickException(f"{path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    click.echo(f"created {path.relative_to(ROOT)}")


@g.command("service")
@click.argument("name")
def g_service(name):
    d = ROOT / "src" / "services" / name
    _write_new(d / "__init__.py", "")
    _write_new(
        d / "service.py",
        f'''from src.core.context import Context


class {name.capitalize()}Service:
    """{name} service."""

    http_exposed = ["get=list"]

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def get_list(self) -> dict:
        return {{"items": [], "total": 0}}
''',
    )
    click.echo(f"→ GET /api/v1/{name}/list (restart the server; strict registrar validates at boot)")


@g.command("model")
@click.argument("name")
def g_model(name):
    _write_new(
        ROOT / "src" / "models" / f"{name}_model.py",
        f'''from sqlalchemy.orm import Mapped

from src.database.base import Base, TimestampMixin, UUIDMixin


class {name.capitalize()}(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "{name}"

    name: Mapped[str]
''',
    )
    click.echo('→ autodiscovered; run: sg db makemigration "add ' + name + '" && sg db migrate')


@g.command("task")
@click.argument("name")
def g_task(name):
    _write_new(
        ROOT / "src" / "tasks" / f"{name}.py",
        f'''"""Tasks are IDEMPOTENT: acks_late redelivers a task whose worker died — running
twice must be safe (natural keys / upserts / dedup guards)."""

from src.tasks.celery_app import task


@task(name="{name}.run")
def run(ctx):
    return {{"ok": True}}
''',
    )


@g.command("script")
@click.argument("name")
@click.option("--kind", type=click.Choice(["once", "repeatable", "manual"]), default="once")
def g_script(name, kind):
    prefix = ""
    if kind == "once":
        existing = [
            int(p.stem.split("_")[0])
            for p in (ROOT / "src" / "scripts").glob("[0-9]*.py")
            if p.stem.split("_")[0].isdigit()
        ]
        prefix = f"{max(existing, default=0) + 1:04d}_"
    _write_new(
        ROOT / "src" / "scripts" / f"{prefix}{name}.py",
        f'''from src.core.context import Context
from src.core.scripts import BaseScript


class Script(BaseScript):
    kind = "{kind}"
    description = "{name}"

    async def run(self, ctx: Context) -> dict:
        return {{"done": True}}
''',
    )


# ---------------- db ----------------

@cli.group()
def db():
    """Migrations and database helpers."""


def _alembic(*args):
    import os
    import subprocess

    env = dict(os.environ)
    env.setdefault("DATABASE_URL", _settings().database_url)
    raise SystemExit(subprocess.call(["alembic", *args], cwd=ROOT, env=env))


@db.command()
def migrate():
    """alembic upgrade head."""
    _alembic("upgrade", "head")


@db.command()
@click.argument("message")
def makemigration(message):
    """alembic revision --autogenerate."""
    _alembic("revision", "--autogenerate", "-m", message)


@db.command("grant-readonly")
def grant_readonly():
    """Print the SQL for the data-views read-only role (run it as a superuser)."""
    dbname = _settings().database_url.rsplit("/", 1)[-1] or "app"
    click.echo(f"""-- data views run through this role: SELECT-only, read-only, time-limited
CREATE ROLE singularity_ro LOGIN PASSWORD 'change-me';
GRANT CONNECT ON DATABASE {dbname} TO singularity_ro;
GRANT USAGE ON SCHEMA public, singularity TO singularity_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public, singularity TO singularity_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO singularity_ro;
ALTER ROLE singularity_ro SET default_transaction_read_only = on;
ALTER ROLE singularity_ro SET statement_timeout = '10s';
-- then set DATAVIEWS_DB_URL=postgresql+asyncpg://singularity_ro:...@host/{dbname}""")


# ---------------- scripts ----------------

@cli.group()
def script():
    """Tracked operational scripts (08)."""


@script.command("run")
@click.argument("name", required=False)
@click.option("--pending", "run_all", is_flag=True, help="deploy step: ordered once + changed repeatables")
@click.option("--force", is_flag=True)
def script_run(name, run_all, force):
    from src.core.schema import ensure_schema
    from src.core.scripts import discover, run_pending, run_script

    async def go():
        await ensure_schema()
        if run_all:
            ran = await run_pending(_ctx(), triggered_by="cli")
            click.echo(f"ran: {ran or 'nothing pending'}")
        else:
            if not name:
                raise click.ClickException("script name required (or --pending)")
            s = next((s for s in discover() if s.name == name), None)
            if s is None:
                raise click.ClickException(f"no script named {name}")
            click.echo(await run_script(s, _ctx(), triggered_by="cli", force=force))

    _run(go())


@script.command("ls")
def script_ls():
    from sqlalchemy import text

    from src.core.schema import ensure_schema
    from src.core.scripts import discover, pending

    async def go():
        await ensure_schema()
        found = discover()
        pend = {p.name for p in await pending(found)}
        from src.database.engine import get_engine

        async with get_engine().connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT DISTINCT ON (name) name, status, started_at FROM "
                    "singularity.script_run ORDER BY name, started_at DESC"
                )
            )
            last = {r.name: (r.status, str(r.started_at)[:19]) for r in rows}
        for s in found:
            status = "pending" if s.name in pend else last.get(s.name, ("never-run", ""))[0]
            click.echo(f" {s.name:<40} {s.kind:<12} {status}")

    _run(go())


@script.command("history")
@click.argument("name")
def script_history(name):
    from sqlalchemy import text

    async def go():
        from src.database.engine import get_engine

        async with get_engine().connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT status, started_at, duration_ms, error, trace_id FROM "
                    "singularity.script_run WHERE name=:n ORDER BY started_at DESC LIMIT 20"
                ),
                {"n": name},
            )
            for r in rows:
                click.echo(f" {str(r.started_at)[:19]} {r.status:<8} {r.duration_ms or 0}ms {r.error or ''} {r.trace_id or ''}")

    _run(go())


# ---------------- config sync ----------------

@cli.command("config")
@click.argument("action", type=click.Choice(["sync"]))
@click.option("--check", is_flag=True, help="fail if the committed template drifted")
def config_cmd(action, check):
    """Generate .env.template from the Settings class — never hand-edit it."""
    from src.config.settings import Settings

    lines = ["# Generated by `sg config sync` from src/config/settings.py — do not hand-edit."]
    for name, field in Settings.model_fields.items():
        desc = field.description or ""
        default = field.default
        if default is None or repr(default) == "PydanticUndefined":
            default = ""
        if isinstance(default, list):
            default = orjson.dumps(default).decode()
        lines.append(f"# {desc}")
        lines.append(f"{name.upper()}={default}")
    content = "\n".join(lines) + "\n"
    template = ROOT / ".env.template"
    if check:
        if template.read_text() != content:
            click.echo("✗ .env.template drifted from Settings — run: sg config sync", err=True)
            raise SystemExit(1)
        click.echo("✓ .env.template matches Settings")
    else:
        template.write_text(content)
        click.echo(f"wrote {template}")


# ---------------- api snapshot ----------------

@cli.command("api")
@click.argument("action", type=click.Choice(["snapshot"]))
@click.option("--check", is_flag=True, help="fail if the schema changed without a snapshot update")
def api_cmd(action, check):
    """OpenAPI snapshot gate: updating the snapshot in the same PR is the explicit act."""
    from src.app import app

    schema = orjson.dumps(app.openapi(), option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2)
    snap = ROOT / "openapi.json"
    if check:
        if not snap.exists() or snap.read_bytes() != schema:
            click.echo("✗ openapi.json drifted — run: sg api snapshot (and review the diff)", err=True)
            raise SystemExit(1)
        click.echo("✓ openapi.json matches the running schema")
    else:
        snap.write_bytes(schema)
        click.echo(f"wrote {snap}")


# ---------------- errors export ----------------

@cli.command("errors")
@click.argument("action", type=click.Choice(["export"]))
def errors_cmd(action):
    """Stable error-code catalog (the contract file frontends consume)."""
    # error classes register on import — pull in every framework module that defines them
    import src.app  # noqa: F401
    import src.auth.protocol  # noqa: F401
    import src.obs.dashboard  # noqa: F401
    from src.core.errors import _codes

    catalog = {
        code: {"status": cls.status, "message": (cls.__doc__ or "").strip()}
        for code, cls in sorted(_codes.items())
    }
    click.echo(orjson.dumps(catalog, option=orjson.OPT_INDENT_2).decode())


# ---------------- tasks dead-letter ----------------

@cli.group()
def tasks():
    """Task queue helpers."""


@tasks.command("dead")
@click.argument("action", type=click.Choice(["ls", "retry"]))
def tasks_dead(action):
    import redis as redis_sync

    from src.tasks.celery_app import DEAD_LIST, celery_app

    r = redis_sync.from_url(_settings().redis_url)
    items = [orjson.loads(x) for x in r.lrange(DEAD_LIST, 0, -1)]
    if action == "ls":
        for it in items:
            click.echo(f" {it['name']:<30} {it['task_id']} {it['error'][:60]}")
        click.echo(f"{len(items)} dead task(s)")
    else:
        for it in items:
            celery_app.send_task(it["name"])  # args lost to repr — retry is by-name
            click.echo(f"re-sent {it['name']}")
        r.delete(DEAD_LIST)


# ---------------- trace viewer ----------------

@cli.command()
@click.argument("trace_id")
@click.option("--lines", is_flag=True, help="show per-line variable state")
def trace(trace_id, lines):
    """Terminal journey viewer (06)."""
    from sqlalchemy import text

    async def go():
        from src.database.engine import get_engine

        async with get_engine().connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT name, status, duration_ms, attributes FROM singularity.records "
                        "WHERE kind='journey' AND trace_id=:t LIMIT 1"
                    ),
                    {"t": trace_id},
                )
            ).first()
        if row is None:
            raise click.ClickException("trace not found (rotated out?)")
        j = row.attributes if isinstance(row.attributes, dict) else orjson.loads(row.attributes)
        click.echo(click.style(f"{j.get('method')} {row.name} → {row.status} ({row.duration_ms}ms)", bold=True))
        if j.get("error"):
            click.echo(click.style(f"  error: {j['error']}", fg="red"))
        for s in j.get("steps", []):
            click.echo(f"  +{s['t']:<9} {s['kind']:<11} {s['name']}" + (f" ({s['duration_ms']}ms)" if s.get("duration_ms") is not None else ""))

        def walk(nodes, depth):
            for n in nodes:
                click.echo("  " + "  " * depth + f"ƒ {n['name']} ({n.get('duration_ms', '?')}ms) → {str(n.get('exc') or n.get('ret'))[:60]}")
                if lines:
                    for ln in n.get("lines", []):
                        if ln.get("vars"):
                            click.echo("  " + "  " * (depth + 1) + f"L{ln['n']}: " + ", ".join(f"{k}={str(v)[:30]}" for k, v in ln["vars"].items()))
                walk(n.get("children", []), depth + 1)

        walk(j.get("calls", []), 0)

    _run(go())


# ---------------- replay ----------------

@cli.command()
@click.argument("trace_id")
@click.option("--base", default="http://localhost:8000", help="target instance")
@click.option("--auth", "auth_token", default="", help="bearer token for the replayed request")
@click.option("--yes", is_flag=True, help="required for non-GET replays against non-localhost")
def replay(trace_id, base, auth_token, yes):
    """Re-fire a recorded request (06). Credentials are never stored or replayed —
    re-authenticate with --auth or target a dev instance using AUTH_DEV_PRINCIPAL."""
    from sqlalchemy import text

    async def go():
        from src.database.engine import get_engine

        async with get_engine().connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT name, attributes FROM singularity.records "
                        "WHERE kind='journey' AND trace_id=:t LIMIT 1"
                    ),
                    {"t": trace_id},
                )
            ).first()
        if row is None:
            raise click.ClickException("trace not found (rotated out?)")
        j = row.attributes if isinstance(row.attributes, dict) else orjson.loads(row.attributes)
        method = j.get("method", "GET")
        if method not in ("GET", "HEAD") and "localhost" not in base and "127.0.0.1" not in base and not yes:
            raise click.ClickException("non-GET replay against a non-local target needs --yes")

        import httpx

        url = base.rstrip("/") + j.get("path", row.name)
        if j.get("query_string"):
            url += "?" + j["query_string"]
        headers = {"X-Replay-Of": trace_id}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        body = j.get("body", "")
        if body:
            headers["Content-Type"] = "application/json"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.request(method, url, headers=headers, content=body or None)
        click.echo(f"{method} {url} → {r.status_code} ({r.elapsed.total_seconds() * 1000:.0f}ms)")
        click.echo(f"original: {j.get('status')} ({j.get('duration_ms')}ms)")
        click.echo(r.text[:500])
        if not body and method not in ("GET", "HEAD"):
            click.echo(click.style(
                "note: no body was recorded for this journey (unarmed at capture time)", fg="yellow"
            ))

    _run(go())


# ---------------- views export/import ----------------

@cli.command("views")
@click.argument("action", type=click.Choice(["export", "import"]))
def views_cmd(action):
    """Round-trip saved view specs ↔ views/*.json (git is the source of truth)."""
    from sqlalchemy import text

    vdir = ROOT / "views"

    async def go():
        from src.database.engine import get_engine

        if action == "export":
            vdir.mkdir(exist_ok=True)
            async with get_engine().connect() as conn:
                rows = await conn.execute(text("SELECT id, name, spec FROM singularity.view"))
                for r in rows:
                    spec = r.spec if isinstance(r.spec, dict) else orjson.loads(r.spec)
                    (vdir / f"{r.id}.json").write_bytes(
                        orjson.dumps({"id": r.id, "name": r.name, "spec": spec}, option=orjson.OPT_INDENT_2)
                    )
                    click.echo(f"exported views/{r.id}.json")
        else:
            async with get_engine().begin() as conn:
                for path in sorted(vdir.glob("*.json")):
                    doc = orjson.loads(path.read_bytes())
                    await conn.execute(
                        text(
                            "INSERT INTO singularity.view (id, name, spec) VALUES (:i, :n, :s) "
                            "ON CONFLICT (id) DO UPDATE SET name=:n, spec=:s, updated_at=now()"
                        ),
                        {"i": doc["id"], "n": doc["name"], "s": orjson.dumps(doc["spec"]).decode()},
                    )
                    click.echo(f"imported {path.name}")

    _run(go())


def main():
    cli()
