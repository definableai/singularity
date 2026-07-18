"""Data views tests (09): guarded executor, inference, suggestion table."""

import asyncio

import pytest

from src.config.settings import settings
from src.obs import dataviews as dv
from tests.test_db import _pg_reachable

pytestmark = pytest.mark.skipif(
    not (_pg_reachable() and settings.dataviews_db_url), reason="postgres/ro-role not available"
)

DSN = None


def setup_module():
    global DSN
    DSN = settings.dataviews_db_url


def _q(sql, **kw):
    return asyncio.run(dv.run_query(DSN, sql, **kw))


def test_select_works_and_reports_types():
    r = _q("SELECT 1::int8 AS n, 'a'::text AS s, now() AS ts")
    assert r["cols"] == [("n", "int8"), ("s", "text"), ("ts", "timestamptz")]
    assert r["rows"][0][0] == 1
    assert r["ms"] > 0


def test_writes_rejected_by_readonly_txn():
    with pytest.raises(dv.DataViewsError, match="read-only"):
        _q("CREATE TABLE _dv_smuggle (id int)")
    with pytest.raises(dv.DataViewsError):
        _q("INSERT INTO api_key (key_hash, prefix, name, principal_id) VALUES ('x','x','x','x')")


def test_multi_statement_rejected():
    with pytest.raises(dv.DataViewsError):
        _q("SELECT 1; SELECT 2")


def test_row_cap_and_truncated_flag():
    r = _q("SELECT generate_series(1, 100)", limit=10)
    assert len(r["rows"]) == 10
    assert r["truncated"] is True


def test_telemetry_schema_queryable():
    r = _q("SELECT kind, count(*) FROM singularity.records GROUP BY 1")
    assert ("kind", "text") in r["cols"]  # SQL over your own traces — free


def test_inference_roles():
    cols = [("day", "timestamptz"), ("user_id", "int8"), ("region", "text"), ("revenue", "numeric")]
    rows = [["2026-07-01", 1, "us", 10.0], ["2026-07-02", 2, "eu", 12.0]] * 5
    inferred = {c["col"]: c for c in dv.infer(cols, rows)}
    assert inferred["day"]["role"] == "time"
    assert inferred["user_id"]["role"] == "dimension"  # id-like demoted from measure
    assert inferred["region"]["role"] == "dimension"
    assert inferred["revenue"]["role"] == "measure"
    assert inferred["revenue"]["format"] == "currency"


@pytest.mark.parametrize(
    "cols,rows,expect",
    [
        ([("total", "numeric")], 1, "big-number"),
        ([("day", "date"), ("revenue", "numeric")], 30, "line"),
        ([("region", "text"), ("revenue", "numeric")], 5, "bar"),
        ([("a", "numeric"), ("b", "numeric")], 100, "scatter"),
        ([("note", "text")], 100, "table"),
    ],
)
def test_suggestion_decision_table(cols, rows, expect):
    inferred = dv.infer(
        cols,
        [[("x" + str(i)) if t == "text" else i for _, t in cols] for i in range(min(rows, 50))],
    )
    assert dv.suggest(inferred, rows)["kind"] == expect


def test_query_endpoint_shapes(client):
    r = client.post(
        "/__obs/api/proto/query", json={"sql": "SELECT 'us' AS region, 42::int8 AS orders"}
    )
    d = r.json()
    assert r.status_code == 200, d
    assert d["inferRows"][0]["col"] == "region"
    assert d["rows"][0][0] == "us"
    assert d["spec"]["chart"]["kind"] == "bar"
    assert "region" in d["spec"]["columns"]

    bad = client.post("/__obs/api/proto/query", json={"sql": "DROP TABLE api_key"})
    assert bad.status_code == 400
    assert "read-only" in bad.json()["error"]


def test_save_view_appears_in_bootstrap(client):
    r = client.post("/__obs/api/proto/query", json={"sql": "SELECT 'x' AS d, 1::int8 AS m"})
    d = r.json()
    save = client.post(
        "/__obs/api/views",
        json={
            "name": "Test View",
            "spec": d["spec"],
            "rows": d["rows"],
            "row_count": d["row_count"],
        },
    )
    assert save.json()["ok"] is True
    boot = client.get("/__obs/api/bootstrap").json()
    assert any(q["name"] == "Test View" for q in boot.get("QUERIES", {}).values())

    import psycopg

    with psycopg.connect(settings.database_url.replace("+asyncpg", ""), autocommit=True) as conn:
        conn.execute("DELETE FROM singularity.view WHERE id='test-view'")
