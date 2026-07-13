"""PG store + fingerprint + RED rollup tests."""


import pytest

from src.obs.store import fingerprint
from tests.test_db import _pg_reachable

TRACE = '''Traceback (most recent call last):
  File "/app/src/services/orders/service.py", line 42, in post_create
    total = compute(order)
  File "/app/src/services/orders/pricing.py", line 9, in compute
    raise ValueError("negative total")
ValueError: negative total
'''


def test_fingerprint_stable_across_line_changes():
    moved = TRACE.replace("line 42", "line 99").replace("line 9", "line 123")
    assert fingerprint("ValueError", TRACE) == fingerprint("ValueError", moved)


def test_fingerprint_differs_by_type_and_path():
    other_type = fingerprint("KeyError", TRACE)
    other_path = fingerprint("ValueError", TRACE.replace("pricing.py", "tax.py"))
    base = fingerprint("ValueError", TRACE)
    assert base != other_type and base != other_path


def test_fingerprint_message_fallback():
    a = fingerprint("log", "", fallback="db connect failed host=x")
    assert a == fingerprint("log", "", fallback="db connect failed host=x")
    assert a != fingerprint("log", "", fallback="something else")


def test_red_rollup_aggregates():
    from src.obs import red

    red.drain()  # reset
    for ms in (10, 20, 30):
        red.observe("request", "/api/v1/orders", "GET", "2xx", ms)
    red.observe("request", "/api/v1/orders", "GET", "5xx", 100)
    out = red.drain()
    assert len(out) == 2  # one per status class
    ok = next(o for o in out if o["extra"]["status_class"] == "2xx")
    assert ok["extra"]["count"] == 3
    assert ok["extra"]["avg_ms"] == 20
    assert ok["extra"]["max_ms"] == 30
    assert red.drain() == []  # drained


@pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")
def test_store_writes_and_issue_lifecycle():
    import psycopg

    from src.config.settings import settings
    from src.obs.store import PGStore

    import uuid

    dsn = settings.database_url.replace("+asyncpg", "")
    store = PGStore(dsn)
    ts = "2026-07-13T10:00:00+00:00"
    run = uuid.uuid4().hex[:8]  # records accumulate across suite runs — unique ids

    def journey_envelope(trace_id):
        return {
            "kind": "journey", "ts": ts, "message": "POST /orders",
            "ctx": {"trace_id": trace_id, "request_id": "req_1", "principal_id": "u1"},
            "extra": {
                "path": "/orders", "status": 500, "duration_ms": 12.5, "error": "ValueError: negative total",
                "steps": [{"kind": "exception", "name": "ValueError", "data": {"trace": TRACE}}],
            },
        }

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        fp = fingerprint("ValueError", TRACE)
        cur.execute("DELETE FROM singularity.issue WHERE fingerprint=%s", (fp,))

        store.write([journey_envelope(f"trc_{run}_a"), journey_envelope(f"trc_{run}_b")])
        assert store.write_errors == 0

        cur.execute(
            "SELECT state, event_count, cardinality(sample_trace_ids) "
            "FROM singularity.issue WHERE fingerprint=%s", (fp,),
        )
        state, count, samples = cur.fetchone()
        assert (state, count, samples) == ("unresolved", 2, 2)

        # resolve, then a new event arrives → regressed (the one lifecycle rule)
        cur.execute("UPDATE singularity.issue SET state='resolved' WHERE fingerprint=%s", (fp,))
        store.write([journey_envelope(f"trc_{run}_c")])
        cur.execute("SELECT state, event_count FROM singularity.issue WHERE fingerprint=%s", (fp,))
        assert cur.fetchone() == ("regressed", 3)

        cur.execute(
            "SELECT count(*) FROM singularity.records "
            "WHERE fingerprint=%s AND kind='journey' AND trace_id LIKE %s", (fp, f"trc_{run}%"),
        )
        assert cur.fetchone()[0] == 3

        cur.execute("DELETE FROM singularity.issue WHERE fingerprint=%s", (fp,))


@pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")
def test_store_survives_pg_bounce():
    from src.config.settings import settings
    from src.obs.store import PGStore

    store = PGStore(settings.database_url.replace("+asyncpg", ""))
    good = {"kind": "log", "ts": "2026-07-13T10:00:00+00:00", "level": "INFO",
            "message": "x", "ctx": {}, "extra": {}, "logger": {"module": "m"}}
    store.write([good])
    assert store.write_errors == 0
    store._conn.close()  # simulate dropped connection
    store.write([good])  # reconnects (or counts) — never raises
    store.write([good])
    assert store.write_errors <= 1  # at most the one bounced batch


def test_fingerprint_message_fallback_strips_volatile():
    a = fingerprint("log", "", fallback="conn <Connection object at 0x11246a80> died, retry 3")
    b = fingerprint("log", "", fallback="conn <Connection object at 0x7fff11aa> died, retry 17")
    assert a == b  # addresses and counters must not mint new issues
