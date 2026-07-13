"""sg CLI tests (07)."""

import pytest
from click.testing import CliRunner

from src.cli.main import ROOT, cli
from tests.test_db import _pg_reachable


def test_config_sync_check_roundtrip():
    r = CliRunner().invoke(cli, ["config", "sync"])
    assert r.exit_code == 0
    r = CliRunner().invoke(cli, ["config", "sync", "--check"])
    assert r.exit_code == 0
    # drift → check fails
    template = ROOT / ".env.template"
    original = template.read_text()
    try:
        template.write_text(original + "HAND_EDIT=1\n")
        r = CliRunner().invoke(cli, ["config", "sync", "--check"])
        assert r.exit_code == 1
    finally:
        template.write_text(original)


def test_api_snapshot_check_roundtrip():
    r = CliRunner().invoke(cli, ["api", "snapshot"])
    assert r.exit_code == 0
    assert CliRunner().invoke(cli, ["api", "snapshot", "--check"]).exit_code == 0
    snap = ROOT / "openapi.json"
    original = snap.read_bytes()
    try:
        snap.write_bytes(b"{}")
        assert CliRunner().invoke(cli, ["api", "snapshot", "--check"]).exit_code == 1
    finally:
        snap.write_bytes(original)


def test_errors_export_catalog():
    import json

    r = CliRunner().invoke(cli, ["errors", "export"])
    assert r.exit_code == 0
    catalog = json.loads(r.output)
    assert catalog["not_found"]["status"] == 404
    assert catalog["auth_failed"]["status"] == 401


def test_generators_create_and_refuse_overwrite(tmp_path, monkeypatch):
    import src.cli.main as m

    monkeypatch.setattr(m, "ROOT", tmp_path)
    (tmp_path / "src" / "scripts").mkdir(parents=True)
    r = CliRunner().invoke(cli, ["g", "service", "orders"])
    assert r.exit_code == 0
    svc = (tmp_path / "src" / "services" / "orders" / "service.py").read_text()
    assert "class OrdersService" in svc and 'http_exposed = ["get=list"]' in svc

    assert CliRunner().invoke(cli, ["g", "service", "orders"]).exit_code != 0  # exists

    CliRunner().invoke(cli, ["g", "script", "seed", "--kind", "once"])
    CliRunner().invoke(cli, ["g", "script", "backfill", "--kind", "once"])
    names = sorted(p.name for p in (tmp_path / "src" / "scripts").glob("*.py"))
    assert names == ["0001_seed.py", "0002_backfill.py"]  # numbering continues


@pytest.mark.skipif(not _pg_reachable(), reason="postgres not reachable")
def test_doctor_green():
    r = CliRunner().invoke(cli, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "✓ db" in r.output
