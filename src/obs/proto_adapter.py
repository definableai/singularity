"""Store → prototype-shape adapter (09).

The Observatory prototype's class-field constants ARE the API contract; this module
maps singularity.* rows into exactly those shapes. Memory fields (mem/FLAME/ALLOCS)
stay empty until 06's memory track lands — empty states, never mocked numbers.
"""

import linecache
import re
from datetime import datetime, timezone
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2]  # repo root — engine records repo-relative files


def _ago(dt) -> str:
    if dt is None:
        return ""
    now = datetime.now(timezone.utc)
    s = int((now - dt).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _hms(ts) -> str:
    return str(ts)[11:19]


def _source_line(file: str, n: int) -> str:
    for base in (_SRC_ROOT / "src", _SRC_ROOT):
        line = linecache.getline(str(base / file), n)
        if line:
            return line.rstrip()[:160]
    return ""


# ---------- bootstrap shapes ----------

def traces(rows) -> list[dict]:
    out = []
    for r in rows:
        attrs = r["attributes"] or {}
        spans = len(attrs.get("steps", [])) + _count_calls(attrs.get("calls", []))
        out.append(
            {
                "id": r["trace_id"],
                "route": f"{attrs.get('method', '')} {r['name']}".strip(),
                "status": int(r["status"] or 0),
                "dur": r["duration_ms"] or 0,
                "spans": spans,
                "user": r["principal_id"] or "—",
                "ts": _hms(r["ts"]),
            }
        )
    return out


def _count_calls(nodes) -> int:
    return sum(1 + _count_calls(n.get("children", [])) for n in nodes)


def logs(rows) -> list[dict]:
    lvl_map = {"WARNING": "WARN", "CRITICAL": "ERROR"}
    return [
        {
            "id": i + 1,
            "ts": str(r["ts"])[11:23],
            "lvl": lvl_map.get(r["level"], r["level"] or "INFO"),
            "svc": (r["name"] or "").split(".")[-1] or "app",
            "msg": r["message"] or "",
            "trace": r["trace_id"] or "",
            "fields": {
                k: str(v)
                for k, v in (r.get("attributes") or {}).items()
                if isinstance(v, (str, int, float)) and k != "steps"
            },
        }
        for i, r in enumerate(rows)
    ]


def users(rows) -> list[dict]:
    return [
        {
            "id": r["principal_id"],
            "email": r.get("_email") or r["principal_id"],
            "name": r.get("_name") or r["principal_id"],
            "role": "USER",
            "status": "active",
            "reqs": f"{r['reqs']:,}",
            "lastSeen": _ago(r["last_seen"]),
            "created": r.get("_created", ""),
        }
        for r in rows
        if r["principal_id"]
    ] or [{
        "id": "none", "email": "no authenticated requests yet", "name": "—", "role": "—",
        "status": "—", "reqs": "0", "lastSeen": "", "created": "",
    }]  # proto invariant: users view falls back to USERS[0]


PLACEHOLDER_ISSUE = {
    "id": "none", "type": "NoIssues", "msg": "No errors recorded yet", "culprit": "",
    "events": "0", "users": "0", "first": "", "last": "", "state": "resolved",
    "lvl": "info", "trace": "", "spark": [0] * 12,
}


def issues(rows, sparks: dict[str, list[int]]) -> list[dict]:
    # proto invariant: ISSUES[0] must exist (si fallback in renderVals)
    out = []
    for r in rows:
        title = r["title"] or ""
        exc_type, _, msg = title.partition(":")
        out.append(
            {
                "id": r["fingerprint"],
                "type": exc_type.strip() or "Error",
                "msg": msg.strip() or title,
                "culprit": "",
                "events": f"{r['event_count']:,}",
                "users": f"{r['user_count']:,}",
                "first": _ago(r["first_seen"]),
                "last": _ago(r["last_seen"]),
                "state": r["state"],
                "lvl": "error",
                "trace": (r["sample_trace_ids"] or [""])[-1],
                "spark": sparks.get(r["fingerprint"], [0] * 12),
            }
        )
    return out or [dict(PLACEHOLDER_ISSUE)]


def errs(issue_rows) -> list[dict]:
    return [
        {
            "msg": (r["title"] or "")[:120],
            "route": "",
            "trace": (r["sample_trace_ids"] or [""])[-1],
            "count": int(r["event_count"]),
            "ago": _ago(r["last_seen"]),
        }
        for r in issue_rows
    ]


def live(rows) -> list[dict]:
    out = []
    for r in rows:
        if r["kind"] == "journey":
            n = int(r["status"] or 0)
            dot = "err" if n >= 500 else "warn" if n >= 400 else "ok"
            msg = f"{r['name']} ▸ {r['status']} {r['duration_ms']}ms"
        else:
            dot = "err" if r["level"] == "ERROR" else "warn" if r["level"] == "WARNING" else "ok"
            msg = f"{(r['name'] or 'app').split('.')[-1]} ▸ {(r['message'] or '')[:110]}"
        out.append({"ts": _hms(r["ts"]), "dot": dot, "msg": msg})
    return out


def bars(minute_rows) -> list[dict]:
    """40 buckets for the overview throughput chart: h = scaled ok-height, eh = errors."""
    pts = list(minute_rows)[-40:]
    if not pts:
        return [{"h": 0, "eh": 0, "tip": ""} for _ in range(40)]
    peak = max((p["ok"] + p["err"]) for p in pts) or 1
    out = [
        {
            "h": round(((p["ok"] + p["err"]) / peak) * 64),
            "eh": min(round((p["err"] / peak) * 64), 64),
            "tip": f"{str(p['m'])[11:16]} · {p['ok'] + p['err']} req",
        }
        for p in pts
    ]
    return [{"h": 0, "eh": 0, "tip": ""}] * (40 - len(out)) + out


# ---------- trace detail shapes ----------

_KIND_MAP = {"sql": "db", "http": "http", "task_submit": "http", "dependency": "mw",
             "exception": "cpu", "endpoint": "cpu"}


def spans(journey: dict, name: str, status, dur) -> list[dict]:
    total = float(journey.get("duration_ms") or dur or 1)
    out = [
        {"id": "s_root", "p": None, "name": f"{journey.get('method', '')} {journey.get('path', name)}",
         "kind": "req", "a": 0, "b": round(total)}
    ]
    for i, s in enumerate(journey.get("steps", [])):
        a = float(s.get("t", 0))
        b = a + float(s.get("duration_ms") or 0.5)
        out.append(
            {"id": f"st{i}", "p": "s_root", "name": s.get("name", s["kind"])[:80],
             "kind": _KIND_MAP.get(s["kind"], "cpu"), "a": round(a, 1), "b": round(min(b, total), 1)}
        )

    def walk(nodes, parent):
        for j, n in enumerate(nodes):
            sid = f"{parent}c{j}"
            a = float(n.get("t", 0))
            b = a + float(n.get("duration_ms") or 0.5)
            slow = (n.get("duration_ms") or 0) > total * 0.5
            out.append(
                {"id": sid, "p": parent if parent != "s_root_calls" else "s_root",
                 "name": n["name"][:80], "kind": "slow" if slow else "cpu",
                 "a": round(a, 1), "b": round(min(b, total), 1)}
            )
            walk(n.get("children", []), sid)

    walk(journey.get("calls", []), "s_root_calls")
    return out


def exec_tree(journey: dict) -> tuple[dict, list[dict]]:
    """calls tree → EXEC {fid: {...}} + EXEC_TREE [{fid, d}] with source via linecache."""
    exec_map: dict[str, dict] = {}
    tree: list[dict] = []
    counter = 0

    def walk(nodes, depth):
        nonlocal counter
        for n in nodes:
            fid = f"fx{counter}"
            counter += 1
            arg_items = [{"k": k, "v": _val(v)} for k, v in (n.get("args") or {}).items()]
            lines = []
            for entry in n.get("lines", []):
                lines.append(
                    {
                        "n": entry["n"],
                        "code": _source_line(n.get("file", ""), entry["n"]),
                        "ms": f"{entry.get('t', '')}",
                        "locals": [{"k": k, "v": _val(v)} for k, v in (entry.get("vars") or {}).items()],
                    }
                )
            exec_map[fid] = {
                "fn": n["name"],
                "file": f"{n.get('file', '')}:{n.get('line', '')}",
                "dur": f"{n.get('duration_ms', '?')}ms",
                "args": arg_items,
                "argsFull": f"({', '.join(a['k'] for a in arg_items)})",
                "ret": _val(n.get("exc")) if n.get("exc") else _val(n.get("ret")),
                "lines": lines,
            }
            tree.append({"fid": fid, "d": depth})
            walk(n.get("children", []), depth + 1)

    walk(journey.get("calls", []), 0)
    if not exec_map:
        # proto invariants: EXEC.fx1 fallback + every frame has >=1 line
        exec_map["fx0"] = {
            "fn": journey.get("path", "request"), "file": "", "dur": f"{journey.get('duration_ms', 0)}ms",
            "args": [], "argsFull": "()", "ret": "",
            "lines": [{"n": 0, "code": "(no user code recorded for this journey)", "ms": "", "locals": []}],
        }
        tree.append({"fid": "fx0", "d": 0})
    for f in exec_map.values():
        if not f["lines"]:
            f["lines"] = [{"n": 0, "code": "(call recorded at T1 — line capture off)", "ms": "", "locals": []}]
    if "fx1" not in exec_map:  # default state.execSel is 'fx1'
        exec_map["fx1"] = exec_map["fx0"]
    return exec_map, tree


def _val(v) -> str:
    if isinstance(v, dict) and "~type" in v:
        return f"{v['~type']}(len={v.get('len')}) {str(v.get('head', ''))[:80]}"
    return str(v)[:160]


def crumbs(log_rows, journey_ts) -> list[dict]:
    out = []
    for r in log_rows:
        delta = (r["ts"] - journey_ts).total_seconds()
        cat = (r["name"] or "app").split(".")[-1]
        lvl = {"ERROR": "error", "WARNING": "warn"}.get(r["level"], "info")
        out.append({"ts": f"{delta:+.2f}s", "cat": cat, "msg": (r["message"] or "")[:140],
                    "extra": "", "lvl": lvl})
    return out


# ---------- issue detail shapes ----------

_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\w+)')


def stack(trace_text: str, code_roots: list[str]) -> list[dict]:
    frames = []
    for m in _FRAME_RE.finditer(trace_text or ""):
        path, line_no, fn = m.group(1), int(m.group(2)), m.group(3)
        rel = path.split("/src/")[-1] if "/src/" in path else path
        in_app = any(root.removeprefix("src/") in path for root in code_roots) or "/src/" in path
        ctx = []
        for n in range(max(1, line_no - 2), line_no + 1):
            code = _source_line(rel, n) or _source_line(path, n)
            if code:
                ctx.append({"n": n, "code": code, **({"err": True} if n == line_no else {})})
        frames.append(
            {"fn": fn, "loc": f"{rel}:{line_no}", "tag": "IN APP" if in_app else "VENDOR",
             "inApp": in_app, "ctx": ctx}
        )
    frames.reverse()  # proto shows most-recent first
    return frames


def issue_events(rows) -> list[dict]:
    return [
        {"id": f"evt_{r['trace_id'][-8:]}" if r["trace_id"] else "evt", "user": r["principal_id"] or "—",
         "dur": f"{r['duration_ms'] or 0}ms", "ts": _hms(r["ts"]), "trace": r["trace_id"] or ""}
        for r in rows
    ]
