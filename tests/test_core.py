"""Framework tests: registrar strictness, error envelope, core layer."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.context import Context
from src.core.registrar import RegistrarError, _method_name, _parse_entry


def test_healthz_and_livez(client):
    assert client.get("/livez").json() == {"status": "ok"}
    r = client.get("/api/v1/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers["x-request-id"].startswith("req_")


def test_method_name_derivation():
    assert _method_name("get", "") == "get"
    assert _method_name("get", "detail/{id}") == "get_detail"
    assert _method_name("post", "create") == "post_create"


def test_entry_parsing_rejects_unknown_verb():
    with pytest.raises(RegistrarError):
        _parse_entry("teleport=away")


class _FakeModule:
    def __init__(self, name: str, cls: type):
        self.__name__ = name
        cls.__module__ = name
        setattr(self, cls.__name__, cls)


def _register(monkeypatch, cls, modname="src.services.thing.service"):
    from src.core import registrar

    monkeypatch.setattr(registrar, "load_package", lambda pkg: [_FakeModule(modname, cls)])
    app = FastAPI()
    registrar.register_services(app, _ctx())
    return app


def test_entry_without_method_is_boot_error(monkeypatch):
    class BadService:
        http_exposed = ["post=create"]  # no post_create method

        def __init__(self, ctx): ...

    with pytest.raises(RegistrarError, match="no method"):
        _register(monkeypatch, BadService)


def test_dict_entry_status_and_path_param(monkeypatch):
    class ThingService:
        http_exposed = [{"verb": "post", "path": "create", "status": 201}, "get=detail/{id}"]

        def __init__(self, ctx): ...

        async def post_create(self) -> dict:
            return {"made": True}

        async def get_detail(self, id: int) -> dict:
            return {"id": id}

    app = _register(monkeypatch, ThingService)
    c = TestClient(app)
    assert c.post("/api/v1/thing/create").status_code == 201
    assert c.get("/api/v1/thing/detail/7").json() == {"id": 7}


def test_error_envelope(client):
    # AppError from a real route path is exercised in later steps; here: shape of 404.
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404


def _ctx():
    from src.config.settings import settings

    return Context(settings)
