import os

os.environ.setdefault("ENVIRONMENT", "dev")

import pytest
from fastapi.testclient import TestClient


class _FixedPrincipalProvider:
    """Test auth: produces exactly the principal the test asked for — no token minting."""

    name = "test"

    def __init__(self, principal):
        self.principal = principal

    async def authenticate(self, request):
        return self.principal


def _install_auth_helpers(client: TestClient) -> TestClient:
    def as_user(user_id: str, claims: dict | None = None, kind: str = "user"):
        from src.auth import deps
        from src.auth.protocol import Principal

        principal = Principal(id=user_id, kind=kind, claims=claims or {})
        deps._instances["test"] = _FixedPrincipalProvider(principal)

        from src.config.settings import settings

        settings.auth_providers = ["test"]
        return client

    def as_api_key(principal_id: str):
        return as_user(principal_id, kind="api_key")

    client.as_user = as_user
    client.as_api_key = as_api_key
    return client


@pytest.fixture()
def client():
    from src.auth import deps
    from src.config.settings import settings

    original_providers = list(settings.auth_providers)
    deps._instances.clear()  # providers cache settings at init; earlier tests mutate settings
    from src.app import app

    with TestClient(app) as c:
        _stop_flusher()
        yield _install_auth_helpers(c)

    deps._instances.pop("test", None)
    settings.auth_providers = original_providers


def _stop_flusher():
    # Tests assert on pipeline.queue directly; a live flusher would race them.
    from src.obs import get_pipeline

    p = get_pipeline()
    if p is not None and p._thread is not None and p._thread.is_alive():
        p._stop.set()
        p._thread.join(timeout=2)
