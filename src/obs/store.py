"""PG store (09): the flusher's durable sink — singularity.records + issue upsert.

Runs on the flusher thread (sync psycopg). Batched multi-row INSERT, never per-request
(Telescope's documented death). PG down → count + drop, never block serving.
"""

import hashlib
import re

import orjson
import psycopg

# Constants
ATTRIBUTES_CAP_BYTES = 8 * 1024  # PG TOASTs JSONB >2KB; hot fields are real columns
SAMPLE_TRACES_CAP = 10

_FRAME_RE = re.compile(r'File "[^"]*/src/([^"]+)", line \d+, in (\w+)')
_VOLATILE_RE = re.compile(r"0x[0-9a-fA-F]+|\b\d+\b|[0-9a-f]{8}-[0-9a-f-]{27,}")

_INSERT = (
    "INSERT INTO singularity.records "
    "(ts, kind, level, trace_id, request_id, principal_id, name, status, duration_ms, "
    "fingerprint, message, attributes) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_ISSUE_UPSERT = """
INSERT INTO singularity.issue (fingerprint, title, event_count, sample_trace_ids)
VALUES (%s, %s, 1, %s)
ON CONFLICT (fingerprint) DO UPDATE SET
  last_seen = now(),
  event_count = issue.event_count + 1,
  state = CASE WHEN issue.state = 'resolved' THEN 'regressed' ELSE issue.state END,
  sample_trace_ids = CASE
    WHEN cardinality(issue.sample_trace_ids) < %s AND %s != ''
    THEN array_append(issue.sample_trace_ids, %s)
    ELSE issue.sample_trace_ids
  END
"""


def fingerprint(exc_type: str, trace_text: str, fallback: str = "") -> str:
    """sha256(exception type + in-app module.function chain). No line numbers — refactors
    must not split issues. In-app = frames under src/ (framework dirs excluded upstream)."""
    frames = [f"{m.group(1)}:{m.group(2)}" for m in _FRAME_RE.finditer(trace_text)]
    if frames:
        basis = exc_type + "|" + "|".join(frames)
    else:
        # message fallback: strip volatile values (addresses, ids, numbers) — otherwise
        # every interpolated value mints a new issue (unbounded group cardinality)
        basis = exc_type + "|" + _VOLATILE_RE.sub("#", fallback[:200])
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


class PGStore:
    def __init__(self, dsn: str):
        self.dsn = dsn  # plain postgres:// (sync psycopg)
        self._conn: psycopg.Connection | None = None
        self.write_errors = 0

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    def write(self, envelopes: list[dict]) -> None:
        """Called from the flusher thread with envelope dicts. Raises nothing."""
        rows, issues = [], []
        for e in envelopes:
            row, issue = self._map(e)
            rows.append(row)
            if issue:
                issues.append(issue)
        try:
            conn = self._connection()
            with conn.cursor() as cur:
                cur.executemany(_INSERT, rows)
                for title, fp, trace_id in issues:
                    cur.execute(
                        _ISSUE_UPSERT,
                        (
                            fp,
                            title,
                            [trace_id] if trace_id else [],
                            SAMPLE_TRACES_CAP,
                            trace_id,
                            trace_id,
                        ),
                    )
        except Exception:
            self.write_errors += len(rows)
            try:  # force reconnect next batch
                if self._conn is not None:
                    self._conn.close()
            finally:
                self._conn = None

    def _map(self, e: dict) -> tuple[tuple, tuple | None]:
        ctx = e.get("ctx", {})
        extra = e.get("extra", {})
        kind = e["kind"]
        name = status = duration = None
        fp = None
        issue = None

        if kind == "journey":
            name = extra.get("path")
            status = str(extra.get("status", ""))
            duration = int(extra.get("duration_ms") or 0)
            if extra.get("error"):
                exc_type = extra["error"].split(":", 1)[0]
                trace_text = ""
                for step in extra.get("steps", []):
                    if step.get("kind") == "exception":
                        trace_text = step.get("data", {}).get("trace", "")
                        break
                fp = fingerprint(exc_type, trace_text, fallback=extra["error"])
                issue = (extra["error"][:200], fp, ctx.get("trace_id", ""))
        elif kind == "log":
            name = e.get("logger", {}).get("module")
            # orphan ERRORs only: an ERROR inside a request/task already has a failing
            # journey — two issues for one defect otherwise
            if e.get("level") == "ERROR" and not ctx.get("trace_id"):
                fp = fingerprint("log", "", fallback=e.get("message", ""))
                issue = (e.get("message", "")[:200], fp, "")
        else:  # metric | audit
            name = e.get("message")

        attributes = orjson.dumps(extra)
        if len(attributes) > ATTRIBUTES_CAP_BYTES:
            attributes = orjson.dumps({"truncated": True, "bytes": len(attributes)})
        return (
            (
                e["ts"],
                kind,
                e.get("level"),
                ctx.get("trace_id"),
                ctx.get("request_id"),
                ctx.get("principal_id"),
                name,
                status,
                duration,
                fp,
                e.get("message"),
                attributes.decode(),
            ),
            issue,
        )
