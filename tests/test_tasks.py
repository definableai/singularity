"""Task subsystem tests (04): decorator semantics, submit-after-commit, dead-letter shape."""

import pytest

from src.tasks.celery_app import DEAD_LIST, celery_app, submit_task, task


@pytest.fixture(scope="module")
def digest_task():
    calls = []

    @task(name="test.digest", schedule=42)
    def digest(ctx, x=None):
        calls.append((ctx is not None, x))
        return {"x": x}

    return digest, calls


def test_task_defaults_and_beat_registration(digest_task):
    digest, _ = digest_task
    t = celery_app.tasks["test.digest"]
    assert t.acks_late is True
    assert t.max_retries == 3
    assert t.autoretry_for == (Exception,)
    assert t.ignore_result is True
    assert celery_app.conf.beat_schedule["test.digest"] == {"task": "test.digest", "schedule": 42}
    assert celery_app.conf.broker_transport_options["visibility_timeout"] > celery_app.conf.task_time_limit


def test_task_gets_ctx_and_runs_eager(digest_task):
    digest, calls = digest_task
    celery_app.conf.task_always_eager = True
    try:
        digest.apply(kwargs={"x": 7})
    finally:
        celery_app.conf.task_always_eager = False
    assert calls[-1] == (True, 7)


def test_submit_defers_until_commit(monkeypatch):
    """Inside a request with an open session: rollback → never sent; commit → sent."""
    sent = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda name, **kw: sent.append((name, kw.get("headers")))
    )

    from src.tracing import journey as jmod

    # simulate get_db attaching the pending list to the current journey
    j = jmod.start("POST", "/orders", "req_1")
    j.singularity_pending_submits = []

    submit_task("emails.send", "a")
    assert sent == []  # deferred, not sent
    assert [s["kind"] for s in j.steps] == ["task_submit"]
    assert j.steps[0]["data"]["deferred"] is True

    # rollback path: pending list dropped, nothing sent — that's the whole point
    pending = j.singularity_pending_submits
    j.singularity_pending_submits = []
    assert sent == []

    # commit path: registrar wrapper flushes
    for send in pending:
        send()
    assert len(sent) == 1
    assert sent[0][0] == "emails.send"
    assert sent[0][1]["trace_id"] == j.trace_id  # trace propagates into the worker


def test_submit_immediate_bypasses_deferral(monkeypatch):
    sent = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, **kw: sent.append(name))

    from src.tracing import journey as jmod

    j = jmod.start("POST", "/x", "req_2")
    j.singularity_pending_submits = []
    submit_task("emails.now", immediate=True)
    assert sent == ["emails.now"]


def test_submit_outside_request_sends_directly(monkeypatch):
    sent = []
    monkeypatch.setattr(celery_app, "send_task", lambda name, **kw: sent.append(name))

    from src.tracing.journey import _journey_var

    _journey_var.set(None)  # no request context (worker / script)
    submit_task("emails.batch")
    assert sent == ["emails.batch"]


def test_dead_letter_push():
    import socket
    from urllib.parse import urlparse

    from src.config.settings import settings

    u = urlparse(settings.redis_url)
    try:
        socket.create_connection((u.hostname, u.port or 6379), timeout=1).close()
    except OSError:
        pytest.skip("redis not reachable")

    import orjson
    import redis as redis_sync

    from src.tasks import celery_app as mod

    class Sender:
        name = "test.dead"

    r = redis_sync.from_url(settings.redis_url)
    r.delete(DEAD_LIST)
    mod._dead_letter(sender=Sender(), task_id="t1", exception=ValueError("x"), args=(1,), kwargs={})
    item = orjson.loads(r.lpop(DEAD_LIST))
    assert item["name"] == "test.dead" and item["task_id"] == "t1"
    assert "ValueError" in item["error"]
