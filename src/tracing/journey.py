"""Journey (06): the automatic recording of one request. T0 slice.

Framework code appends steps; app authors never touch this — zero author-visible
surface. Emission is decided at request end with full information (emit-on-interesting).
"""

import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_journey_var: ContextVar["Journey | None"] = ContextVar("journey", default=None)

# Constants
MAX_STEPS = 5_000  # budget: past this the journey is marked degraded and stops recording
NPLUS1_THRESHOLD = 10  # same statement shape this many times in one journey → flagged
BODY_CAP_BYTES = 8 * 1024

_SHAPE_RE = re.compile(r"\s+|\b\d+\b")


@dataclass
class Journey:
    trace_id: str
    request_id: str
    method: str
    path: str
    started: float = field(default_factory=time.perf_counter)
    status: int = 0
    error: str | None = None
    degraded: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    # engine fields (06): tier 0=T0 boundaries, 1=call tree, 2=lines
    tier: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    frame_map: dict[int, dict] = field(default_factory=dict)
    engine_events: int = 0
    # replay inputs (06): captured by CoreLayer
    query_string: str = ""
    body: str = ""
    n_plus_one: dict[str, Any] | None = None

    def add_step(self, kind: str, name: str, duration_ms: float | None = None, **data) -> None:
        if len(self.steps) >= MAX_STEPS:
            self.degraded = True
            return
        step: dict[str, Any] = {"kind": kind, "name": name, "t": round((time.perf_counter() - self.started) * 1000, 3)}
        if duration_ms is not None:
            step["duration_ms"] = round(duration_ms, 3)
        if data:
            step["data"] = data
        self.steps.append(step)


def current() -> Journey | None:
    return _journey_var.get()


def start(method: str, path: str, request_id: str) -> Journey:
    j = Journey(trace_id="trc_" + uuid.uuid4().hex[:20], request_id=request_id, method=method, path=path)
    trace_id_var.set(j.trace_id)
    _journey_var.set(j)
    return j


def _strip(node: dict) -> dict:
    node.pop("_prev", None)
    node.pop("_t0", None)
    for child in node.get("children", []):
        _strip(child)
    return node


def detect_n_plus_one(j: Journey) -> None:
    """Fold sql steps by statement shape — the most common ORM production fire, caught
    automatically (06). Same shape ≥ threshold in one journey → flagged, emit-worthy."""
    shapes: dict[str, list[float]] = {}
    for s in j.steps:
        if s["kind"] != "sql":
            continue
        shape = _SHAPE_RE.sub(" ", s["name"]).strip()[:120]
        shapes.setdefault(shape, []).append(s.get("duration_ms") or 0)
    if not shapes:
        return
    worst_shape, durations = max(shapes.items(), key=lambda kv: len(kv[1]))
    if len(durations) >= NPLUS1_THRESHOLD:
        j.n_plus_one = {
            "shape": worst_shape,
            "count": len(durations),
            "total_ms": round(sum(durations), 2),
        }


def finish(j: Journey, status: int) -> dict[str, Any]:
    j.status = status
    detect_n_plus_one(j)
    extra: dict[str, Any] = {
        "method": j.method,
        "path": j.path,
        "status": status,
        "duration_ms": round((time.perf_counter() - j.started) * 1000, 3),
        "degraded": j.degraded,
        "error": j.error,
        "steps": j.steps,
    }
    if j.calls:
        extra["calls"] = [_strip(n) for n in j.calls]  # unclosed frames keep helpers
        extra["tier_used"] = max(j.tier, 1)
    if j.n_plus_one:
        extra["n_plus_one"] = j.n_plus_one
    if j.query_string:
        extra["query_string"] = j.query_string
    if j.body:
        extra["body"] = j.body
    return {"message": f"{j.method} {j.path}", "trace_id": j.trace_id, "extra": extra}


def should_emit(j: Journey, duration_ms: float, mode: str, slow_ms: int) -> bool:
    """dev → all; on_error → failed, slow, errored, or N+1-flagged; off → never."""
    if mode == "dev":
        return True
    if mode == "off":
        return False
    return (
        j.status >= 500
        or j.error is not None
        or duration_ms > slow_ms
        or j.n_plus_one is not None
    )
