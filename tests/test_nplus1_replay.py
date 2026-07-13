"""N+1 detector + replay-input capture tests (06)."""

from src.tracing.journey import NPLUS1_THRESHOLD, Journey, detect_n_plus_one, finish, should_emit


def _journey_with_sql(shapes: list[str]) -> Journey:
    j = Journey(trace_id="t", request_id="r", method="GET", path="/orders")
    for i, stmt in enumerate(shapes):
        j.add_step("sql", stmt, duration_ms=2.0)
    return j


def test_nplus1_flags_repeated_shape():
    stmts = [f"SELECT * FROM line_items WHERE order_id = {i}" for i in range(NPLUS1_THRESHOLD + 2)]
    j = _journey_with_sql(stmts)
    detect_n_plus_one(j)
    assert j.n_plus_one is not None
    assert j.n_plus_one["count"] == NPLUS1_THRESHOLD + 2  # literals normalized to one shape
    assert j.n_plus_one["total_ms"] == (NPLUS1_THRESHOLD + 2) * 2.0


def test_nplus1_ignores_distinct_statements():
    j = _journey_with_sql([f"SELECT * FROM t{i} WHERE a = 1" for i in range(NPLUS1_THRESHOLD + 2)])
    detect_n_plus_one(j)
    assert j.n_plus_one is None  # different tables = different shapes


def test_nplus1_makes_journey_emit_worthy_in_on_error():
    j = _journey_with_sql(["SELECT * FROM x WHERE id = 1"] * (NPLUS1_THRESHOLD + 1))
    j.status = 200
    payload = finish(j, 200)  # finish runs detection
    assert payload["extra"]["n_plus_one"]["count"] == NPLUS1_THRESHOLD + 1
    assert should_emit(j, duration_ms=5, mode="on_error", slow_ms=1000) is True

    healthy = Journey(trace_id="t2", request_id="r", method="GET", path="/x")
    finish(healthy, 200)
    assert should_emit(healthy, duration_ms=5, mode="on_error", slow_ms=1000) is False


def test_replay_inputs_captured(client):
    from src.app import app
    from src.obs import get_pipeline

    @app.post("/api/v1/_echo", include_in_schema=False)
    async def echo(payload: dict) -> dict:
        return payload

    p = get_pipeline()
    p.queue.clear()
    r = client.post("/api/v1/_echo?debug=1", json={"marker": 42})
    assert r.json() == {"marker": 42}
    journeys = [i[3] for i in p.queue if i[0] == "journey"]
    extra = next(j["extra"] for j in journeys if j["extra"]["path"] == "/api/v1/_echo")
    assert extra["query_string"] == "debug=1"
    # dev mode arms every request at T2 → body captured for replay
    assert '"marker"' in extra["body"]
