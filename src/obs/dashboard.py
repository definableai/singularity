"""Observatory (09): the prototype UI, served verbatim, populated with real data.

The proto (`ui/observatory.dc.html` + dc runtime `ui/support.js`) keeps its data as
Component class fields; the runtime evals the inline script — so real data arrives by
a server-injected patch INSIDE that script block: fetch /__obs/api/bootstrap →
Object.assign(this, payload) → setState. traceId/issueId changes fetch detail shapes.
React UMD is vendored; window.__resources redirects the runtime's CDN urls locally.
"""

import asyncio
import secrets
from pathlib import Path

import orjson
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, ORJSONResponse, Response
from sqlalchemy import text
from starlette.responses import StreamingResponse

from src.config.settings import settings
from src.core.errors import AppError
from src.obs import proto_adapter as pa

TAIL_POLL_S = 2.0
TAIL_MAX_CONNECTIONS = 10
SYNC_INTERVAL_MS = 5000

_UI = Path(__file__).parent / "ui"
_tail_connections = 0


class DashboardAuthError(AppError):
    """Dashboard access denied."""

    code = "dashboard_auth"
    status = 401


async def dashboard_auth(request: Request) -> None:
    """Override me (app.dependency_overrides[dashboard_auth] = ...) to hook real auth."""
    if settings.is_dev:
        return
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or (
        request.query_params.get("token", "")
    )
    if not settings.dashboard_token or not secrets.compare_digest(token, settings.dashboard_token):
        raise DashboardAuthError()


router = APIRouter(prefix="/__obs", dependencies=[Depends(dashboard_auth)])


async def _rows(sql: str, params: dict | None = None) -> list[dict]:
    from src.database.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return [dict(r._mapping) for r in result]


# ---------- static: the prototype, transformed at serve time ----------

_PATCH = """
// ---- injected by Singularity: real data wiring (09) ----
const __sgOrigMount = Component.prototype.componentDidMount;
Component.prototype.componentDidMount = function() {
  if (__sgOrigMount) __sgOrigMount.call(this);
  const sync = async () => {
    try {
      const d = await fetch('/__obs/api/bootstrap').then(r => r.json());
      Object.assign(this, d);
      this.setState({});
    } catch (e) { console.warn('observatory sync failed', e); }
  };
  sync();
  this.__sgIv = setInterval(sync, __SYNC_MS__);
  const origSet = this.setState.bind(this);
  this.setState = (patch) => {
    const beforeT = this.state.traceId, beforeI = this.state.issueId;
    origSet(patch);
    queueMicrotask(async () => {
      try {
        if (this.state.traceId && this.state.traceId !== beforeT) {
          const d = await fetch('/__obs/api/proto/trace/' + this.state.traceId).then(r => r.json());
          if (!d.error) { Object.assign(this, d); origSet({}); }
        }
        if (this.state.issueId && this.state.issueId !== beforeI) {
          const d = await fetch('/__obs/api/proto/issue/' + this.state.issueId).then(r => r.json());
          if (!d.error) { Object.assign(this, d); origSet({}); }
        }
      } catch (e) { console.warn('observatory detail fetch failed', e); }
    });
  };
};
const __sgOrigUnmount = Component.prototype.componentWillUnmount;
Component.prototype.componentWillUnmount = function() {
  if (this.__sgIv) clearInterval(this.__sgIv);
  if (__sgOrigUnmount) __sgOrigUnmount.call(this);
};
// trace header + data-views wiring — the proto hardcodes these; rewrites bind them
const __sgOrigVals = Component.prototype.renderVals;
Component.prototype.renderVals = function() {
  const vals = __sgOrigVals.call(this);
  const t = this.TRACES.find(x => x.id === this.state.traceId);
  vals.sgTraceRoute = t ? t.route : '';
  vals.sgTraceMeta = t ? `${t.dur}ms · ${t.spans} spans` : '';
  vals.sgTraceSub = t ? `${t.id} · ${t.ts}` : '';
  vals.sgTraceUser = t ? t.user : '';
  // SQL editor Run → real guarded executor; results land in the proto's own pipeline
  vals.runNew = async () => {
    if (this.state.running) return;
    this.setState({ running: true });
    try {
      const sql = this.state.newSql ?? this.NEW_SQL;
      const r = await fetch('/__obs/api/proto/query', {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({ sql })
      });
      const d = await r.json();
      if (d.error) { alert === undefined || console.error(d.error); this.__sgQueryError = d.error; this.setState({ running: false }); return; }
      this.NEW_ROWS = d.rows;
      this.__sgInfer = d.inferRows;
      this.__sgSpec = JSON.stringify(d.spec, null, 2);
      this.__sgSuggest = d.suggestion;
      this.__sgLast = d;
      this.__sgQueryError = null;
      this.setState({ running: false, newRan: true, ranMs: d.ms });
    } catch (e) { console.error('query failed', e); this.setState({ running: false }); }
  };
  if (this.__sgInfer) vals.inferRows = this.__sgInfer;
  if (this.__sgSpec) vals.specJson = this.__sgSpec;
  if (this.__sgQueryError) vals.specJson = 'ERROR: ' + this.__sgQueryError;
  vals.sgSaveView = async () => {
    if (!this.__sgLast) return;
    const name = prompt ? window.prompt('View name:', 'My view') : 'My view';
    if (!name) return;
    await fetch('/__obs/api/views', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({ name, spec: this.__sgLast.spec, rows: this.__sgLast.rows,
                             row_count: this.__sgLast.row_count })
    });
  };
  return vals;
};
"""

# proto template hardcodes the selected-trace header; bind to patch-provided vals
_TEMPLATE_REWRITES = {
    ">POST /v1/checkout</span>": ">{{ sgTraceRoute }}</span>",
    ">412ms · 18 spans · +9.8 MB</span>": ">{{ sgTraceMeta }}</span>",
    ">tr_9f2ec41a · 14:32:08.412</span>": ">{{ sgTraceSub }}</span>",
    "maya@arclight.io →": "{{ sgTraceUser }} →",
    # the proto's Save view button ships dead — bind it
    'style-hover="opacity:.85">Save view</div>': 'style-hover="opacity:.85" onClick="{{ sgSaveView }}">Save view</div>',
}


async def _stat_tiles() -> dict[str, str]:
    red = await _rows(
        "SELECT sum((attributes->>'count')::int) AS total, "
        "sum(CASE WHEN attributes->>'status_class'='5xx' THEN (attributes->>'count')::int ELSE 0 END) AS errs, "
        "max((attributes->>'max_ms')::float) AS worst "
        "FROM singularity.records WHERE kind='metric' AND message='red.request' "
        "AND ts > now() - interval '1 hour'"
    )
    issues = await _rows(
        "SELECT count(*) AS open, coalesce(sum(event_count),0) AS events FROM singularity.issue "
        "WHERE state IN ('unresolved','regressed')"
    )
    total = red[0]["total"] or 0
    errs = red[0]["errs"] or 0
    return {
        "24.1k": f"{round(total / 60):,}" if total else "0",
        "184ms": f"{round(red[0]['worst'] or 0)}ms",
        "0.42%": f"{(errs / total * 100):.2f}%" if total else "0.00%",
        "101 events · 3 unresolved": f"{issues[0]['events']} events · {issues[0]['open']} unresolved",
        "v0.9.2 · us-east": f"singularity · {settings.environment}",
        "All systems nominal": "All systems nominal" if not issues[0]["open"] else f"{issues[0]['open']} open issues",
    }


@router.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    html = (_UI / "observatory.dc.html").read_text()
    resources = {
        "https://unpkg.com/react@18.3.1/umd/react.production.min.js": "/__obs/vendor/react.js",
        "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js": "/__obs/vendor/react-dom.js",
    }
    html = html.replace(
        '<script src="./support.js"></script>',
        f"<script>window.__resources={orjson.dumps(resources).decode()}</script>"
        '<script src="/__obs/support.js"></script>',
    )
    for literal, real in (await _stat_tiles()).items():
        html = html.replace(literal, real)
    for literal, binding in _TEMPLATE_REWRITES.items():
        html = html.replace(literal, binding)
    patch = _PATCH.replace("__SYNC_MS__", str(SYNC_INTERVAL_MS))
    # append the patch INSIDE the component logic block — the runtime evals exactly the
    # text of <script data-dc-script>; anywhere else is dead code
    marker = html.find("data-dc-script")
    idx = html.find("</script>", marker)
    if marker == -1 or idx == -1:
        raise RuntimeError("observatory.dc.html: no <script data-dc-script> block found")
    html = html[:idx] + patch + html[idx:]
    return HTMLResponse(html)


@router.get("/support.js", include_in_schema=False)
async def support_js() -> Response:
    return Response((_UI / "support.js").read_bytes(), media_type="text/javascript")


@router.get("/vendor/{name}.js", include_in_schema=False)
async def vendor(name: str) -> Response:
    if name not in ("react", "react-dom"):
        return Response(status_code=404)
    return Response((_UI / "vendor" / f"{name}.js").read_bytes(), media_type="text/javascript")


# ---------- bootstrap: everything the list views need, proto-shaped ----------

@router.get("/api/bootstrap", include_in_schema=False)
async def bootstrap():
    trace_rows = await _rows(
        "SELECT ts, name, status, duration_ms, trace_id, principal_id, attributes "
        "FROM singularity.records WHERE kind='journey' ORDER BY ts DESC LIMIT 60"
    )
    log_rows = await _rows(
        "SELECT ts, level, name, message, trace_id, attributes FROM singularity.records "
        "WHERE kind='log' ORDER BY ts DESC LIMIT 200"
    )
    if settings.dashboard_users_sql:
        # the dev's own users table, through the guarded executor (read-only role)
        from src.obs import dataviews as dv

        try:
            uq = await dv.run_query(settings.dataviews_db_url, settings.dashboard_users_sql, limit=200)
            names = [c[0] for c in uq["cols"]]

            def col(row, key):
                return str(row[names.index(key)]) if key in names else ""

            user_rows = [
                {"principal_id": col(r, "id") or col(r, "email"), "reqs": 0, "last_seen": None,
                 "_email": col(r, "email"), "_name": col(r, "name"), "_created": col(r, "created_at")}
                for r in uq["rows"]
            ]
        except Exception:
            user_rows = []
    else:
        user_rows = await _rows(
            "SELECT principal_id, count(*) AS reqs, max(ts) AS last_seen FROM singularity.records "
            "WHERE principal_id IS NOT NULL AND principal_id != '' "
            "GROUP BY 1 ORDER BY reqs DESC LIMIT 50"
        )
    issue_rows = await _rows(
        "SELECT fingerprint, title, state, first_seen, last_seen, event_count, user_count, "
        "sample_trace_ids FROM singularity.issue ORDER BY last_seen DESC LIMIT 100"
    )
    spark_rows = await _rows(
        "SELECT fingerprint, width_bucket(extract(epoch FROM (now() - ts)), 0, 86400, 12) AS b, "
        "count(*) AS n FROM singularity.records "
        "WHERE fingerprint IS NOT NULL AND ts > now() - interval '24 hours' GROUP BY 1, 2"
    )
    sparks: dict[str, list[int]] = {}
    for r in spark_rows:
        arr = sparks.setdefault(r["fingerprint"], [0] * 12)
        b = min(max(int(r["b"]) - 1, 0), 11)
        arr[11 - b] = int(r["n"])  # bucket 1 = most recent → rightmost
    minute_rows = await _rows(
        "SELECT date_trunc('minute', ts) AS m, "
        "sum(CASE WHEN attributes->>'status_class'!='5xx' THEN (attributes->>'count')::int ELSE 0 END) AS ok, "
        "sum(CASE WHEN attributes->>'status_class'='5xx' THEN (attributes->>'count')::int ELSE 0 END) AS err "
        "FROM singularity.records WHERE kind='metric' AND message='red.request' "
        "AND ts > now() - interval '40 minutes' GROUP BY 1 ORDER BY 1"
    )
    live_rows = await _rows(
        "SELECT ts, kind, level, name, message, status, duration_ms FROM singularity.records "
        "WHERE kind IN ('journey','log') ORDER BY ts DESC LIMIT 12"
    )
    view_rows = await _rows("SELECT id, name, spec FROM singularity.view ORDER BY created_at LIMIT 20")
    queries = {}
    for i, r in enumerate(view_rows):
        spec = r["spec"] if isinstance(r["spec"], dict) else orjson.loads(r["spec"])
        queries[f"q{i + 1}"] = {  # proto invariant: keys are q1..qN (state.queryId default 'q1')
            "name": r["name"],
            "meta": spec.get("meta", ""),
            "sql": spec.get("query", {}).get("sql", ""),
            "cols": list(spec.get("columns", {}).keys()),
            "rows": spec.get("last_rows", []),
        }
    open_issues = [r for r in issue_rows if r["state"] in ("unresolved", "regressed")]
    return ORJSONResponse(
        {
            "TRACES": pa.traces(trace_rows),
            "LOGS": pa.logs(log_rows),
            "USERS": pa.users(user_rows),
            "ISSUES": pa.issues(issue_rows, sparks),
            "ERRS": pa.errs(open_issues[:5]),
            "LIVE": pa.live(live_rows),
            "BARS": pa.bars(minute_rows),
            **({"QUERIES": queries} if queries else {}),
        }
    )


@router.get("/api/proto/trace/{trace_id}", include_in_schema=False)
async def proto_trace(trace_id: str):
    rows = await _rows(
        "SELECT ts, name, status, duration_ms, attributes FROM singularity.records "
        "WHERE kind='journey' AND trace_id=:tid LIMIT 1",
        {"tid": trace_id},
    )
    if not rows:
        return ORJSONResponse({"error": "not found"}, status_code=404)
    r = rows[0]
    journey = r["attributes"] if isinstance(r["attributes"], dict) else orjson.loads(r["attributes"])
    log_rows = await _rows(
        "SELECT ts, level, name, message FROM singularity.records "
        "WHERE kind='log' AND trace_id=:tid ORDER BY ts LIMIT 100",
        {"tid": trace_id},
    )
    exec_map, tree = pa.exec_tree(journey)
    return ORJSONResponse(
        {
            "SPANS": pa.spans(journey, r["name"], r["status"], r["duration_ms"]),
            "EXEC": exec_map,
            "EXEC_TREE": tree,
            "CRUMBS": pa.crumbs(log_rows, r["ts"]),
        }
    )


@router.get("/api/proto/issue/{fingerprint}", include_in_schema=False)
async def proto_issue(fingerprint: str):
    rows = await _rows(
        "SELECT fingerprint, title, state, sample_trace_ids FROM singularity.issue "
        "WHERE fingerprint=:f",
        {"f": fingerprint},
    )
    if not rows:
        return ORJSONResponse({"error": "not found"}, status_code=404)
    issue = rows[0]
    events = await _rows(
        "SELECT ts, trace_id, principal_id, duration_ms FROM singularity.records "
        "WHERE fingerprint=:f ORDER BY ts DESC LIMIT 20",
        {"f": fingerprint},
    )
    trace_text = ""
    if issue["sample_trace_ids"]:
        detail = await _rows(
            "SELECT attributes FROM singularity.records WHERE kind='journey' AND trace_id=:tid LIMIT 1",
            {"tid": issue["sample_trace_ids"][-1]},
        )
        if detail:
            attrs = detail[0]["attributes"]
            attrs = attrs if isinstance(attrs, dict) else orjson.loads(attrs)
            for step in attrs.get("steps", []):
                if step.get("kind") == "exception":
                    trace_text = step.get("data", {}).get("trace", "")
                    break
    return ORJSONResponse(
        {
            "STACK": pa.stack(trace_text, settings.trace_code_roots),
            "TAGGROUPS": [],  # release/region tags arrive when the app declares them
            "ISSUE_EVENTS": pa.issue_events(events),
        }
    )


@router.post("/api/proto/query", include_in_schema=False)
async def proto_query(body: dict):
    """SQL editor Run: guarded executor → result + inference + suggestion, proto-shaped."""
    from src.obs import dataviews as dv

    if not settings.dataviews_db_url:
        return ORJSONResponse(
            {"error": "data views disabled — run `sg db grant-readonly`, set DATAVIEWS_DB_URL"},
            status_code=409,
        )
    sql = (body.get("sql") or "").strip().rstrip(";")
    if not sql:
        return ORJSONResponse({"error": "empty sql"}, status_code=422)
    try:
        result = await dv.run_query(settings.dataviews_db_url, sql, limit=1000)
    except dv.DataViewsError as e:
        return ORJSONResponse({"error": str(e)}, status_code=400)
    inferred = dv.infer(result["cols"], result["rows"])
    suggestion = dv.suggest(inferred, len(result["rows"]))

    # proto shapes: inferRows table + 4-tuple bar rows [label, sub, value, w*0.38]
    infer_rows = [
        {
            "col": c["col"], "pg": c["pg"],
            "role": c["role"].upper() + (" · $" if c["format"] == "currency" else ""),
            "rc": "var(--tx)" if c["role"] == "measure" else "var(--tx2)",
            "card": c["card"], "why": c["why"],
        }
        for c in inferred
    ]
    enc = suggestion["encoding"]
    label_col = enc.get("x") or (inferred[0]["col"] if inferred else "")
    value_col = enc.get("y") or (inferred[-1]["col"] if inferred else "")
    names = [c["col"] for c in inferred]
    li = names.index(label_col) if label_col in names else 0
    vi = names.index(value_col) if value_col in names else len(names) - 1
    numeric = [float(r[vi] or 0) for r in result["rows"][:12]] or [1]
    peak = max(numeric) or 1
    bar_rows = [
        [str(r[li]), "", f"{r[vi]:,}" if isinstance(r[vi], (int, float)) else str(r[vi]),
         round(float(r[vi] or 0) / peak * 38)]
        for r in result["rows"][:12]
    ]
    spec = {
        "query": {"sql": sql},
        "columns": {c["col"]: {"type": c["pg"], "role": c["role"], **({"format": c["format"]} if c["format"] else {})} for c in inferred},
        "chart": {"kind": suggestion["kind"], "encoding": enc},
        "limits": {"rows": 1000},
    }
    return ORJSONResponse(
        {
            "rows": bar_rows,
            "inferRows": infer_rows,
            "suggestion": suggestion,
            "spec": spec,
            "ms": result["ms"],
            "truncated": result["truncated"],
            "row_count": len(result["rows"]),
        }
    )


@router.post("/api/views", include_in_schema=False)
async def save_view(body: dict):
    """Save-view button: persist the spec; it appears as a qN tile via bootstrap."""
    import re as _re

    name = (body.get("name") or "Untitled view").strip()[:80]
    spec = body.get("spec")
    if not isinstance(spec, dict) or "query" not in spec:
        return ORJSONResponse({"error": "spec with a query is required"}, status_code=422)
    view_id = _re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "view"
    spec["last_rows"] = body.get("rows", [])[:10]
    spec["meta"] = f"saved · {body.get('row_count', 0)} rows"
    from src.database.engine import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO singularity.view (id, name, spec) VALUES (:i, :n, :s) "
                "ON CONFLICT (id) DO UPDATE SET name=:n, spec=:s, updated_at=now()"
            ),
            {"i": view_id, "n": name, "s": orjson.dumps(spec).decode()},
        )
    return ORJSONResponse({"ok": True, "id": view_id})


@router.post("/api/issues/{fingerprint}/state", include_in_schema=False)
async def set_issue_state(fingerprint: str, body: dict):
    new_state = body.get("state")
    if new_state not in ("resolved", "ignored", "unresolved"):
        return ORJSONResponse({"error": "state must be resolved|ignored|unresolved"}, status_code=422)
    from src.database.engine import get_engine

    async with get_engine().begin() as conn:
        await conn.execute(
            text("UPDATE singularity.issue SET state=:s WHERE fingerprint=:f"),
            {"s": new_state, "f": fingerprint},
        )
    return ORJSONResponse({"ok": True})


@router.get("/api/tail", include_in_schema=False)
async def tail(request: Request, kinds: str = "log,journey"):
    """SSE live tail — polls the shared PG store (multi-worker safe by construction)."""
    global _tail_connections
    if _tail_connections >= TAIL_MAX_CONNECTIONS:
        return ORJSONResponse({"error": "too many tail connections"}, status_code=429)
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()]

    async def stream():
        global _tail_connections
        _tail_connections += 1
        try:
            last_ts = (await _rows("SELECT now() AS n"))[0]["n"]
            while not await request.is_disconnected():
                rows = await _rows(
                    "SELECT ts, kind, level, name, message, trace_id, status, duration_ms "
                    "FROM singularity.records "
                    "WHERE ts > :last AND kind = ANY(:kinds) ORDER BY ts LIMIT 200",
                    {"last": last_ts, "kinds": kind_list},
                )
                for r in rows:
                    last_ts = max(last_ts, r["ts"])
                    yield f"data: {orjson.dumps({**r, 'ts': str(r['ts'])}).decode()}\n\n"
                yield ": keepalive\n\n"
                await asyncio.sleep(TAIL_POLL_S)
        finally:
            _tail_connections -= 1

    return StreamingResponse(stream(), media_type="text/event-stream")
