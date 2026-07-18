"""Data views (09): the guarded SQL executor + inference + chart suggestion.

Defense in depth: dedicated read-only role (DATAVIEWS_DB_URL — sg db grant-readonly),
read-only transaction, per-txn statement_timeout, single-statement execution (asyncpg's
extended protocol rejects multi-statement strings natively), row cap by cursor fetch
(never SQL rewriting — wrapping arbitrary SQL in LIMIT breaks CTEs and changes plans).
"""

import re
import time

import asyncpg

# Constants
ROW_CAP = 10_000
SAMPLE_FOR_CARDINALITY = 500
STATEMENT_TIMEOUT = "10s"
POOL_MAX = 2  # dashboard queries can never exhaust anything

_pools: dict[int, asyncpg.Pool] = {}  # keyed by event loop — pools are loop-bound


class DataViewsError(Exception):
    pass


async def _get_pool(dsn: str) -> asyncpg.Pool:
    import asyncio

    key = id(asyncio.get_running_loop())
    if key not in _pools:
        _pools[key] = await asyncpg.create_pool(
            dsn.replace("+asyncpg", ""), min_size=0, max_size=POOL_MAX
        )
    return _pools[key]


async def run_query(dsn: str, sql: str, limit: int = ROW_CAP) -> dict:
    """→ {cols, rows, truncated, ms}. Raises DataViewsError with the PG message."""
    pool = await _get_pool(dsn)
    t0 = time.perf_counter()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                stmt = await conn.prepare(sql)  # extended protocol: one statement only
                attrs = stmt.get_attributes()
                rows = []
                async with conn.transaction():  # cursor needs a (nested) transaction
                    async for record in stmt.cursor():
                        rows.append(list(record))
                        if len(rows) > limit:
                            break
    except asyncpg.PostgresError as e:
        raise DataViewsError(str(e)) from None
    truncated = len(rows) > limit
    return {
        "cols": [(a.name, a.type.name) for a in attrs],
        "rows": rows[:limit],
        "truncated": truncated,
        "ms": round((time.perf_counter() - t0) * 1000, 1),
    }


# ---------- inference: pg type (mechanical) + semantic role (heuristic, overridable) ----------

_TIME_TYPES = {"timestamp", "timestamptz", "date", "timetz", "time"}
_NUM_TYPES = {"int2", "int4", "int8", "float4", "float8", "numeric", "money", "oid"}
_ID_RE = re.compile(r"(^id$|_id$|^uuid$|_uuid$)")


def infer(cols: list[tuple[str, str]], rows: list[list]) -> list[dict]:
    sample = rows[:SAMPLE_FOR_CARDINALITY]
    out = []
    for i, (name, pg_type) in enumerate(cols):
        values = [r[i] for r in sample if r[i] is not None]
        distinct = len({str(v) for v in values})
        card = f"{distinct}" + (f" ({distinct / len(values):.0%})" if values else "")
        if pg_type in _TIME_TYPES:
            role, why = "time", "temporal type → x axis"
        elif _ID_RE.search(name.lower()):
            role, why = "dimension", "id-like name → identifier, not a quantity"
        elif pg_type in _NUM_TYPES:
            role, why = "measure", f"numeric ({pg_type}) → aggregatable"
        elif values and distinct <= max(30, len(values) // 10):
            role, why = "dimension", "low cardinality, non-numeric → group-by axis"
        else:
            role, why = "dimension", "non-numeric"
        fmt = (
            "currency"
            if any(k in name.lower() for k in ("revenue", "price", "amount", "total"))
            and role == "measure"
            else ""
        )
        out.append(
            {"col": name, "pg": pg_type, "role": role, "format": fmt, "card": card, "why": why}
        )
    return out


# ---------- chart suggestion: the deterministic decision table (no solver) ----------


def suggest(inferred: list[dict], row_count: int) -> dict:
    times = [c for c in inferred if c["role"] == "time"]
    measures = [c for c in inferred if c["role"] == "measure"]
    dims = [c for c in inferred if c["role"] == "dimension"]

    def enc(x=None, y=None, series=None):
        return {"x": x, "y": y, "series": series}

    if len(measures) == 1 and row_count == 1 and not times and not dims:
        return {
            "kind": "big-number",
            "encoding": enc(y=measures[0]["col"]),
            "why": "1 measure, 1 row",
        }
    if times and measures:
        series = dims[0]["col"] if dims else None
        return {
            "kind": "line",
            "encoding": enc(times[0]["col"], measures[0]["col"], series),
            "why": "time + measure → line" + (" per " + series if series else ""),
        }
    if dims and measures:
        if len(dims) >= 2:
            return {
                "kind": "bar",
                "encoding": enc(dims[0]["col"], measures[0]["col"], dims[1]["col"]),
                "why": "2 dimensions + measure → grouped bars",
            }
        return {
            "kind": "bar",
            "encoding": enc(dims[0]["col"], measures[0]["col"]),
            "why": "dimension + measure → bars, sorted desc",
        }
    if len(measures) >= 2:
        return {
            "kind": "scatter",
            "encoding": enc(measures[0]["col"], measures[1]["col"]),
            "why": "2 measures → scatter",
        }
    return {
        "kind": "table",
        "encoding": enc(),
        "why": "no chartable shape → table (the safe fallback)",
    }
