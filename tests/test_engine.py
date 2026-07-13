"""sys.monitoring engine tests (06): call tree, args/returns, lines, budget, breaker."""

import asyncio

import pytest

from src.tracing import engine
from src.tracing import journey as jmod


@pytest.fixture()
def armed_engine(monkeypatch):
    """Engine initialized with THIS test file as the code root."""
    if engine._tool_id is None:
        ok = engine.init([__file__.rsplit("/", 1)[0]])  # tests/ dir as root
        assert ok, "no free tool id (coverage running?)"
    else:
        monkeypatch.setattr(engine, "_roots", (__file__.rsplit("/", 1)[0],))
        engine._is_user_code.clear()
    monkeypatch.setattr(engine, "_tripped", False)
    monkeypatch.setattr(engine, "_errors", 0)
    yield engine
    engine._is_user_code.clear()


# --- traced target functions (live under the test root) ---

def _price(order: dict, factor: float) -> float:
    subtotal = order["amount"] * factor
    tax = subtotal * 0.2
    total = subtotal + tax
    return total


def _build(amount: int) -> dict:
    order = {"amount": amount}
    order["total"] = _price(order, 1.5)
    return order


async def _async_endpoint(x: int) -> int:
    await asyncio.sleep(0.001)
    a = _sync_helper(x)
    await asyncio.sleep(0.001)
    return a + 1


def _sync_helper(x: int) -> int:
    y = x * 2
    return y


def _boom():
    raise ValueError("engine test")


def _run_traced(fn, *args, tier=2):
    j = jmod.start("GET", "/engine-test", "req_e")
    engine.arm(j, tier=tier)
    try:
        result = fn(*args)
    finally:
        engine.disarm(j)
    return j, result


def test_call_tree_args_returns_lines(armed_engine):
    j, result = _run_traced(_build, 4)
    assert result == {"amount": 4, "total": 7.2}

    roots = [n["name"] for n in j.calls]
    assert "_build" in str(roots)
    build = next(n for n in j.calls if n["name"].endswith("_build"))
    assert build["args"] == {"amount": 4}
    assert build["duration_ms"] >= 0
    assert build["ret"]["~type"] == "dict"

    # transitive discovery: _price recorded as child, no registration anywhere
    price = next(c for c in build["children"] if c["name"].endswith("_price"))
    assert price["args"]["factor"] == 1.5
    assert price["ret"] == 7.2

    # T2: line entries with variable diffs — the flight recorder
    lines = price["lines"]
    assert len(lines) >= 3
    all_vars = {}
    for entry in lines:
        all_vars.update(entry.get("vars", {}))
    assert all_vars["subtotal"] == 6.0
    assert all_vars["tax"] == 6.0 * 0.2  # engine records true float values
    assert all_vars["total"] == 7.2


def test_t1_records_calls_but_no_lines(armed_engine):
    j, _ = _run_traced(_build, 2, tier=1)
    build = next(n for n in j.calls if n["name"].endswith("_build"))
    assert "lines" not in build
    assert build["children"]


def test_async_call_tree_survives_awaits(armed_engine):
    async def flow():
        j = jmod.start("GET", "/async-test", "req_a")
        engine.arm(j, tier=1)
        try:
            r = await _async_endpoint(5)
        finally:
            engine.disarm(j)
        return j, r

    j, r = asyncio.run(flow())
    assert r == 11
    node = next(n for n in j.calls if n["name"].endswith("_async_endpoint"))
    assert node["ret"] == 11
    helper = next(c for c in node["children"] if c["name"].endswith("_sync_helper"))
    assert helper["args"] == {"x": 5} and helper["ret"] == 10


def test_exception_closes_frame_with_exc(armed_engine):
    j = jmod.start("GET", "/boom", "req_b")
    engine.arm(j, tier=1)
    try:
        with pytest.raises(ValueError):
            _boom()
    finally:
        engine.disarm(j)
    node = next(n for n in j.calls if n["name"].endswith("_boom"))
    assert "ret" not in node
    assert node["exc"].startswith("ValueError")
    assert node["raise"].startswith("ValueError")


def test_budget_degrades_tier_not_silently(armed_engine, monkeypatch):
    monkeypatch.setattr(engine, "MAX_EVENTS", 5)
    j, _ = _run_traced(_build, 1)
    assert j.degraded is True
    assert j.tier < 2  # degrade ladder ran


def test_unarmed_journey_records_nothing(armed_engine):
    j = jmod.start("GET", "/off", "req_o")  # tier stays 0, engine.arm not called
    _build(3)
    assert j.calls == []


def test_stdlib_never_recorded(armed_engine):
    j, _ = _run_traced(sorted, [3, 1, 2])
    assert j.calls == []  # builtins/stdlib are not user code


def test_finish_payload_includes_stripped_tree(armed_engine):
    j, _ = _run_traced(_build, 4)
    payload = jmod.finish(j, 200)
    calls = payload["extra"]["calls"]
    assert payload["extra"]["tier_used"] >= 1

    def no_helpers(node):
        assert "_prev" not in node and "_t0" not in node
        for c in node["children"]:
            no_helpers(c)

    for n in calls:
        no_helpers(n)
