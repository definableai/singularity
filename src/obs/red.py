"""In-process RED counters (05). The core layer (requests) and @task wrapper (tasks)
increment; the flusher drains one rollup per active key per window."""

import threading
import time

WINDOW_S = 10.0

_lock = threading.Lock()
_counters: dict[tuple, list] = {}  # key → [count, error_count, sum_ms, max_ms]


def observe(kind: str, name: str, method: str, status_class: str, duration_ms: float) -> None:
    key = (kind, name, method, status_class)
    with _lock:
        c = _counters.get(key)
        if c is None:
            _counters[key] = [1, status_class == "5xx", duration_ms, duration_ms]
        else:
            c[0] += 1
            c[1] += status_class == "5xx"
            c[2] += duration_ms
            c[3] = max(c[3], duration_ms)


def drain() -> list[dict]:
    with _lock:
        snapshot, _counters_copy = dict(_counters), _counters.clear()
    out = []
    for (kind, name, method, status_class), (count, errors, sum_ms, max_ms) in snapshot.items():
        out.append(
            {
                "message": f"red.{kind}",
                "extra": {
                    "name": name,
                    "method": method,
                    "status_class": status_class,
                    "count": count,
                    "errors": int(errors),
                    "avg_ms": round(sum_ms / count, 2),
                    "max_ms": round(max_ms, 2),
                    "window_s": WINDOW_S,
                    "ts_window": int(time.time() // WINDOW_S * WINDOW_S),
                },
            }
        )
    return out
