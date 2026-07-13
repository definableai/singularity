"""Scripts subsystem tests (08) — require compose PG; skipped when unreachable."""

import asyncio
import textwrap

import pytest
from sqlalchemy import text

from tests.test_db import _pg_reachable

pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")


@pytest.fixture()
def scripts_env(tmp_path, monkeypatch):
    """Fresh script dir + clean script_run rows + schema ensured, all on one loop."""
    import sys

    from src.config.settings import settings
    from src.core.context import Context

    sdir = tmp_path / "scripts"
    sdir.mkdir()
    (sdir / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def write(name: str, kind: str, body: str = "return {'ok': True}"):
        (sdir / f"{name}.py").write_text(
            textwrap.dedent(f"""
            from src.core.scripts import BaseScript

            class Script(BaseScript):
                kind = "{kind}"
                async def run(self, ctx):
                    {body}
            """)
        )
        sys.modules.pop(f"src.scripts.{name}", None)

    import src.core.scripts as scripts_mod

    def discover_here():
        # point discovery at the tmp dir but keep real import machinery
        import importlib.util
        import hashlib

        found = []
        for path in sorted(sdir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"tmp_scripts.{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cls = module.Script
            prefix = path.stem.split("_")[0]
            order = int(prefix) if prefix.isdigit() else -1
            if cls.kind == "once" and order < 0:
                raise scripts_mod.ScriptError(f"{path}: once needs numeric prefix")
            found.append(
                scripts_mod.Discovered(
                    path.stem, cls.kind, hashlib.sha256(path.read_bytes()).hexdigest(), cls, order
                )
            )
        return found

    async def reset():
        from src.core.schema import ensure_schema
        from src.database.engine import get_engine

        await get_engine().dispose()
        await ensure_schema()
        async with get_engine().begin() as conn:
            await conn.execute(text("DELETE FROM singularity.script_run"))
        await get_engine().dispose()

    asyncio.run(reset())
    return write, discover_here, Context(settings), scripts_mod


def _run(coro):
    async def with_dispose():
        from src.database.engine import get_engine

        try:
            return await coro
        finally:
            await get_engine().dispose()

    return asyncio.run(with_dispose())


def test_once_runs_exactly_once(scripts_env):
    write, discover, ctx, m = scripts_env
    write("0001_seed", "once")

    async def flow():
        scripts = discover()
        first = await m.pending(scripts)
        assert [s.name for s in first] == ["0001_seed"]
        await m.run_script(scripts[0], ctx, "test")
        return await m.pending(discover())

    assert _run(flow()) == []


def test_once_drift_is_hard_error(scripts_env):
    write, discover, ctx, m = scripts_env
    write("0001_seed", "once")

    async def flow():
        await m.run_script(discover()[0], ctx, "test")
        write("0001_seed", "once", "return {'ok': 2}")  # edit after success
        await m.check_drift(discover())

    with pytest.raises(m.ScriptError, match="drift"):
        _run(flow())


def test_repeatable_reruns_on_checksum_change(scripts_env):
    write, discover, ctx, m = scripts_env
    write("reindex", "repeatable")

    async def flow():
        await m.run_script(discover()[0], ctx, "test")
        same = await m.pending(discover())
        write("reindex", "repeatable", "return {'v': 2}")
        changed = await m.pending(discover())
        return [s.name for s in same], [s.name for s in changed]

    same, changed = _run(flow())
    assert same == []
    assert changed == ["reindex"]


def test_failed_run_recorded_and_retried(scripts_env):
    write, discover, ctx, m = scripts_env
    write("0001_boom", "once", "raise ValueError('nope')")

    async def flow():
        from src.database.engine import get_engine

        with pytest.raises(m.ScriptError, match="failed"):
            await m.run_script(discover()[0], ctx, "test")
        async with get_engine().connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT status, error FROM singularity.script_run WHERE name='0001_boom'")
                )
            ).first()
        still_pending = await m.pending(discover())
        return row, [s.name for s in still_pending]

    row, still = _run(flow())
    assert row[0] == "failed" and "nope" in row[1]
    assert still == ["0001_boom"]  # failed row doesn't count as done


def test_manual_never_pending(scripts_env):
    write, discover, ctx, m = scripts_env
    write("fix_orders", "manual")
    assert _run(m.pending(discover())) == []


def test_ensure_schema_idempotent_and_concurrent():
    from src.core.schema import ensure_schema

    async def both():
        from src.database.engine import get_engine

        await get_engine().dispose()
        await asyncio.gather(ensure_schema(), ensure_schema())
        await get_engine().dispose()

    asyncio.run(both())  # no duplicate-key explosions
