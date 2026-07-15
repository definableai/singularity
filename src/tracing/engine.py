"""sys.monitoring engine (06, PEP 669): automatic transitive call/line capture.

Design facts this is built on (CPython docs):
- Local events (LINE) are per code object; exception + call-boundary events we use
  globally (PY_START/PY_RETURN/PY_UNWIND/RAISE) toggle with the armed-journey refcount.
- PY_RETURN does not fire on exception exits — PY_UNWIND closes those frames.
- The call tree is built by FRAME IDENTITY (f_back chain), not a stack: async
  interleaving cannot corrupt it, and awaits need no YIELD/RESUME bookkeeping.
- Every callback body is caught: a tracer bug increments a counter and trips the
  circuit breaker — the tracer must be provably unable to alter app behavior.
- Idle cost: at refcount 0 global events are off and LINE callbacks self-extinguish
  via DISABLE; arming a new journey calls restart_events(). Zero when never traced,
  near-zero (one restart) when re-arming.
"""

import sys
import time
from typing import Any

from src.tracing.journey import Journey, current

mon = sys.monitoring  # Python ≥ 3.12 (PEP 669) — pinned by the framework

# Constants
MAX_EVENTS = 5_000
MAX_STR = 200
MAX_CONTAINER_HEAD = 3
BREAKER_MAX_ERRORS = 50

_tool_id: int | None = None
_armed_refcount = 0
_is_user_code: dict[Any, bool] = {}  # code object → classification (cached forever)
_roots: tuple[str, ...] = ()
_errors = 0
_tripped = False

GLOBAL_EVENTS = 0  # filled at init


def init(code_roots: list[str]) -> bool:
    """Claim a tool id and register callbacks. False → T0-only (loud, not silent)."""
    global _tool_id, _roots, GLOBAL_EVENTS
    _roots = tuple(code_roots)
    for candidate in (3, 4):
        try:
            mon.use_tool_id(candidate, "singularity-tracer")
            _tool_id = candidate
            break
        except ValueError:
            continue
    if _tool_id is None:
        return False
    E = mon.events
    GLOBAL_EVENTS = E.PY_START | E.PY_RETURN | E.PY_UNWIND | E.RAISE
    mon.register_callback(_tool_id, E.PY_START, _on_start)
    mon.register_callback(_tool_id, E.PY_RETURN, _on_return)
    mon.register_callback(_tool_id, E.PY_UNWIND, _on_unwind)
    mon.register_callback(_tool_id, E.RAISE, _on_raise)
    mon.register_callback(_tool_id, E.LINE, _on_line)
    return True


KILL_SWITCH_KEY = "singularity:trace:kill"
QUEUE_HIGH_WATERMARK = 0.8  # obs queue depth past this → shed T2, then T1 (keep T0)

_killed = False  # refreshed by a background task — never a per-request Redis GET


async def kill_switch_refresher(interval_s: float = 2.0) -> None:
    """Manual prod kill switch: `redis-cli SET singularity:trace:kill 1` — effective
    within ~2s, no deploy. Redis down = last-known value (01's degradation rules)."""
    global _killed
    import asyncio

    from src.common.redis import get_redis

    while True:
        try:
            _killed = bool(await get_redis().get(KILL_SWITCH_KEY))
        except Exception:
            pass  # keep last-known value
        await asyncio.sleep(interval_s)


def _backpressure_tier(tier: int) -> int:
    """Recording never competes with serving traffic: obs queue over the high-watermark
    sheds T2 first, then T1 (06)."""
    from src.obs import get_pipeline
    from src.obs.pipeline import QUEUE_SIZE

    p = get_pipeline()
    if p is None:
        return tier
    depth = len(p.queue) / QUEUE_SIZE
    if depth > QUEUE_HIGH_WATERMARK:
        return 0
    if depth > QUEUE_HIGH_WATERMARK / 2 and tier == 2:
        return 1
    return tier


def arm(journey: Journey, tier: int) -> None:
    global _armed_refcount
    if _tool_id is None or _tripped or _killed or tier < 1:
        return
    tier = _backpressure_tier(tier)
    if tier < 1:
        return
    journey.tier = tier
    journey.frame_map = {}
    journey.calls = []
    _armed_refcount += 1
    if _armed_refcount == 1:
        mon.set_events(_tool_id, GLOBAL_EVENTS)
        mon.restart_events()  # revive LINE events that self-extinguished while idle


def disarm(journey: Journey) -> None:
    global _armed_refcount
    if _tool_id is None or getattr(journey, "tier", 0) < 1:
        return
    journey.tier = 0
    _armed_refcount = max(0, _armed_refcount - 1)
    if _armed_refcount == 0:
        mon.set_events(_tool_id, 0)


def _classify(code) -> bool:
    cached = _is_user_code.get(code)
    if cached is None:
        f = code.co_filename
        cached = any(root in f for root in _roots)
        _is_user_code[code] = cached
    return cached


def _trip(e: BaseException) -> None:
    global _errors, _tripped
    _errors += 1
    if _errors >= BREAKER_MAX_ERRORS and not _tripped:
        _tripped = True
        try:
            mon.set_events(_tool_id, 0)
            print(f"tracer circuit breaker TRIPPED after {_errors} errors: {e!r}", file=sys.stderr)
        except Exception:
            pass


def _ser(value: Any) -> Any:
    """Type-dispatched, hard-capped. Never raises."""
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return value if len(value) <= MAX_STR else value[:MAX_STR] + "…"
        if isinstance(value, (list, tuple, set, frozenset)):
            head = [_ser(v) for i, v in enumerate(value) if i < MAX_CONTAINER_HEAD]
            return {"~type": type(value).__name__, "len": len(value), "head": head}
        if isinstance(value, dict):
            head = {
                str(k): _ser(v) for i, (k, v) in enumerate(value.items()) if i < MAX_CONTAINER_HEAD
            }
            return {"~type": "dict", "len": len(value), "head": head}
        r = repr(value)
        return f"<{type(value).__name__}> {r[:MAX_STR]}"
    except Exception:
        return f"<{type(value).__name__}: unserializable>"


def _journey_wants(tier_needed: int) -> Journey | None:
    j = current()
    if j is None or getattr(j, "tier", 0) < tier_needed:
        return None
    return j


def _budget(j: Journey) -> bool:
    j.engine_events = getattr(j, "engine_events", 0) + 1
    if j.engine_events > MAX_EVENTS:
        if j.tier == 2:
            j.tier = 1  # degrade ladder: T2 → T1
            j.degraded = True
        elif j.tier == 1:
            j.tier = 0
            j.degraded = True
        return False
    return True


def _on_start(code, offset):
    try:
        if not _classify(code):
            return
        j = _journey_wants(1)
        if j is None or not _budget(j):
            return
        frame = sys._getframe(1)
        args = {}
        try:
            for name, val in frame.f_locals.items():
                if name != "self":
                    args[name] = _ser(val)
        except Exception:
            args = {"~error": "locals unavailable"}
        node = {
            "name": code.co_qualname,
            "file": code.co_filename.rsplit("/src/", 1)[-1],
            "line": code.co_firstlineno,
            "t": round((time.perf_counter() - j.started) * 1000, 3),
            "args": args,
            "children": [],
        }
        if j.tier >= 2:
            node["lines"] = []
            node["_prev"] = dict.fromkeys(args, None)
            mon.set_local_events(_tool_id, code, mon.events.LINE)
        parent = j.frame_map.get(id(frame.f_back))
        (parent["children"] if parent else j.calls).append(node)
        j.frame_map[id(frame)] = node
        node["_t0"] = time.perf_counter()
    except Exception as e:
        _trip(e)


def _close(code, retval, exc: str | None):
    try:
        if not _classify(code):
            return
        j = _journey_wants(1)
        if j is None:
            return
        frame = sys._getframe(2)  # _close ← _on_return/_on_unwind ← monitored frame
        node = j.frame_map.pop(id(frame), None)
        if node is None:
            return
        node["duration_ms"] = round(
            (time.perf_counter() - node.pop("_t0", time.perf_counter())) * 1000, 3
        )
        node.pop("_prev", None)
        if exc is not None:
            node["exc"] = exc
        else:
            node["ret"] = _ser(retval)
    except Exception as e:
        _trip(e)


def _on_return(code, offset, retval):
    _close(code, retval, None)


def _on_unwind(code, offset, exc):
    _close(code, None, f"{type(exc).__name__}: {exc}"[:MAX_STR])


def _on_raise(code, offset, exc):
    try:
        if not _classify(code):
            return
        j = _journey_wants(1)
        if j is None:
            return
        frame = sys._getframe(1)
        node = j.frame_map.get(id(frame))
        if node is not None and "raise" not in node:
            node["raise"] = f"{type(exc).__name__}: {exc}"[:MAX_STR]
    except Exception as e:
        _trip(e)


def _on_line(code, line_number):
    try:
        if _armed_refcount == 0:
            return mon.DISABLE  # self-extinguish while idle; restart_events() revives
        if not _classify(code):
            return mon.DISABLE
        j = _journey_wants(2)
        if j is None:
            return
        if not _budget(j):
            return
        frame = sys._getframe(1)
        node = j.frame_map.get(id(frame))
        if node is None or "lines" not in node:
            return
        prev = node.get("_prev", {})
        changed = {}
        try:
            for name, val in frame.f_locals.items():
                s = _ser(val)
                if prev.get(name) != s:
                    changed[name] = s
                    prev[name] = s
        except Exception:
            pass
        entry = {"n": line_number, "t": round((time.perf_counter() - j.started) * 1000, 3)}
        if changed:
            entry["vars"] = changed
        node["lines"].append(entry)
        node["_prev"] = prev
    except Exception as e:
        _trip(e)
