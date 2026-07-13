"""Observatory tests (09): auth gate, API shapes, self-exclusion."""

import pytest

from src.config.settings import settings
from tests.test_db import _pg_reachable

pytestmark = pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")


def test_dashboard_serves_proto_ui(client):
    r = client.get("/__obs/")
    assert r.status_code == 200
    assert "x-dc" in r.text  # the prototype runtime shell
    assert "injected by Singularity" in r.text  # data patch landed inside the script
    assert "/__obs/vendor/react.js" in r.text  # CDN redirected to vendored react
    assert client.get("/__obs/support.js").status_code == 200
    assert client.get("/__obs/vendor/react.js").status_code == 200


def test_auth_gate_outside_dev(client, monkeypatch):
    import asyncio

    from src.obs.dashboard import DashboardAuthError, dashboard_auth

    class Req:
        headers = {}
        query_params = {}

    monkeypatch.setattr(settings, "environment", "prod")
    monkeypatch.setattr(settings, "dashboard_token", "s3cret")
    with pytest.raises(DashboardAuthError):
        asyncio.run(dashboard_auth(Req()))

    class Authed:
        headers = {"authorization": "Bearer s3cret"}
        query_params = {}

    asyncio.run(dashboard_auth(Authed()))  # no raise

    # no token configured → always denied outside dev
    monkeypatch.setattr(settings, "dashboard_token", "")
    with pytest.raises(DashboardAuthError):
        asyncio.run(dashboard_auth(Authed()))


def test_self_exclusion_no_journeys_for_dashboard(client):
    from src.obs import get_pipeline

    p = get_pipeline()
    p.queue.clear()
    client.get("/__obs/api/issues")
    kinds = [i[0] for i in p.queue]
    assert "journey" not in kinds  # dashboard requests are never traced


def test_bootstrap_shapes(client):
    d = client.get("/__obs/api/bootstrap").json()
    assert set(d) >= {"TRACES", "LOGS", "USERS", "ISSUES", "ERRS", "LIVE", "BARS"}
    assert len(d["BARS"]) == 40
    for t in d["TRACES"][:3]:
        assert set(t) >= {"id", "route", "status", "dur", "spans", "user", "ts"}
    for i in d["ISSUES"][:3]:
        assert len(i["spark"]) == 12
    if d["TRACES"]:
        tid = d["TRACES"][0]["id"]
        detail = client.get(f"/__obs/api/proto/trace/{tid}").json()
        assert set(detail) >= {"SPANS", "EXEC", "EXEC_TREE", "CRUMBS"}
        assert detail["SPANS"][0]["kind"] == "req"


def test_issue_state_transition(client):
    import uuid

    import psycopg

    fp = "testfp_" + uuid.uuid4().hex[:8]
    dsn = settings.database_url.replace("+asyncpg", "")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO singularity.issue (fingerprint, title) VALUES (%s, 't')", (fp,))
    r = client.post(f"/__obs/api/issues/{fp}/state", json={"state": "resolved"})
    assert r.json() == {"ok": True}
    items = client.get("/__obs/api/bootstrap").json()["ISSUES"]
    assert any(i["id"] == fp and i["state"] == "resolved" for i in items)
    assert client.post(f"/__obs/api/issues/{fp}/state", json={"state": "bogus"}).status_code == 422

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM singularity.issue WHERE fingerprint=%s", (fp,))
