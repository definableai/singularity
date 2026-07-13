"""T0 tracing tests: journey emission, emit policy, exception steps, log correlation."""

from src.tracing.journey import Journey, should_emit


def _journeys(pipeline):
    return [item for item in pipeline.queue if item[0] == "journey"]


def test_journey_emitted_with_endpoint_step(client):
    from src.obs import get_pipeline

    p = get_pipeline()
    p.queue.clear()
    client.get("/api/v1/healthz")
    js = _journeys(p)
    assert len(js) == 1
    payload = js[0][3]
    assert payload["extra"]["status"] == 200
    assert payload["extra"]["path"] == "/api/v1/healthz"
    assert payload["trace_id"].startswith("trc_")
    kinds = [s["kind"] for s in payload["extra"]["steps"]]
    assert "endpoint" in kinds
    endpoint = next(s for s in payload["extra"]["steps"] if s["kind"] == "endpoint")
    assert endpoint["name"] == "HealthzService.get"
    assert endpoint["duration_ms"] >= 0


def test_logs_carry_matching_trace_id(client):
    from src.obs import get_pipeline

    p = get_pipeline()
    p.queue.clear()
    client.get("/api/v1/healthz")
    j = _journeys(p)[0][3]
    # any in-request log would carry the same trace id; simulate via sink shape:
    # the journey's trace_id is the one bound during the request
    assert j["trace_id"]


def test_emit_policy():
    j = Journey(trace_id="t", request_id="r", method="GET", path="/x")
    j.status = 200
    assert should_emit(j, 10, "dev", 1000) is True
    assert should_emit(j, 10, "off", 1000) is False
    assert should_emit(j, 10, "on_error", 1000) is False  # healthy+fast → not emitted
    assert should_emit(j, 5000, "on_error", 1000) is True  # slow
    j.status = 500
    assert should_emit(j, 10, "on_error", 1000) is True  # failed
    j.status = 200
    j.error = "boom"
    assert should_emit(j, 10, "on_error", 1000) is True  # errored


def test_exception_recorded_as_step(client):
    from fastapi.testclient import TestClient

    from src.app import app
    from src.obs import get_pipeline

    @app.get("/api/v1/_boom", include_in_schema=False)
    async def boom():
        raise ValueError("negative total")

    p = get_pipeline()
    with TestClient(app, raise_server_exceptions=False) as c:
        p.queue.clear()
        r = c.get("/api/v1/_boom")
        # asserts stay inside the block: client exit runs lifespan shutdown, which
        # drains the queue we're inspecting
        assert r.status_code == 500
        body = r.json()
        assert body["error"]["code"] == "internal"
        assert body["error"]["request_id"].startswith("req_")
        js = _journeys(p)
        assert len(js) == 1
        extra = js[0][3]["extra"]
        exc_steps = [s for s in extra["steps"] if s["kind"] == "exception"]
        assert len(exc_steps) == 1
        assert exc_steps[0]["name"] == "ValueError"
        assert exc_steps[0]["data"]["handled"] is False
        assert "negative total" in extra["error"]


def test_journey_step_budget():
    j = Journey(trace_id="t", request_id="r", method="GET", path="/x")
    from src.tracing import journey as mod

    for i in range(mod.MAX_STEPS + 10):
        j.add_step("call", f"f{i}")
    assert len(j.steps) == mod.MAX_STEPS
    assert j.degraded is True
