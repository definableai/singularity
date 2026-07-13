"""Obs pipeline tests: capture → envelope → transport, redaction, loss accounting."""

import orjson

from src.obs.pipeline import Pipeline


class CollectTransport:
    def __init__(self):
        self.events = []

    def send(self, batch):
        self.events.extend(orjson.loads(e) for e in batch)


class BoomTransport:
    def send(self, batch):
        raise RuntimeError("down")


def _pipeline(*transports):
    return Pipeline(list(transports), {"password", "token"}, {"app": "t", "environment": "dev"})


def test_envelope_shape_and_redaction():
    sink = CollectTransport()
    p = _pipeline(sink)
    p.enqueue(
        "log",
        {
            "level": "INFO",
            "message": "login",
            "trace_id": "trc_1",
            "extra": {"password": "hunter2", "nested": {"token": "x", "ok": 1}, "user": "u1"},
        },
    )
    p._drain()
    (e,) = sink.events
    assert e["v"] == 1 and e["kind"] == "log" and e["message"] == "login"
    assert e["ctx"]["trace_id"] == "trc_1"
    assert e["extra"]["password"] == "[redacted]"
    assert e["extra"]["nested"]["token"] == "[redacted]"
    assert e["extra"]["nested"]["ok"] == 1
    assert e["extra"]["user"] == "u1"


def test_transport_failure_isolated():
    sink = CollectTransport()
    p = _pipeline(BoomTransport(), sink)
    p.enqueue("metric", {"message": "m", "extra": {"value": 1}})
    p._drain()  # must not raise
    assert p.transport_errors == 1
    assert len(sink.events) == 1  # healthy transport still got the batch


def test_overflow_counts_drops():
    p = _pipeline(CollectTransport())
    from src.obs import pipeline as mod

    for _ in range(mod.QUEUE_SIZE + 50):
        p.enqueue("log", {"message": "x"})
    assert p.dropped == 50
    assert len(p.queue) == mod.QUEUE_SIZE


def test_metric_and_audit_kinds(client):
    # init() ran in lifespan; log.metric/log.audit exist and enqueue with right kinds
    from src.common.logger import log
    from src.obs import get_pipeline

    p = get_pipeline()
    p.queue.clear()
    log.metric("orders.created", 3, route="/x")
    log.audit("order.refunded", entity="order:1", amount=50)
    kinds = [item[0] for item in p.queue]
    assert kinds == ["metric", "audit"]


def test_capture_sink_receives_log_calls(client):
    from src.common.logger import log
    from src.obs import get_pipeline

    p = get_pipeline()
    p.queue.clear()
    log.info("hello obs", foo="bar")
    assert any(item[0] == "log" and item[3]["message"] == "hello obs" for item in p.queue)
