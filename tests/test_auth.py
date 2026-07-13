"""Auth tests (03): jwt hardening, api_key + caches, chain, dev principal."""

import asyncio
import time

import jwt as pyjwt
import pytest

from src.auth.protocol import AuthError, Principal, bearer_token
from src.config.settings import settings

SECRET = "test-secret"


class FakeRequest:
    """Duck-types Request/WebSocket for provider-level tests."""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def _jwt_provider(monkeypatch, **over):
    from src.auth.providers.jwt_provider import JwtProvider

    monkeypatch.setattr(settings, "jwt_secret", over.get("secret", SECRET))
    monkeypatch.setattr(settings, "jwt_jwks_url", over.get("jwks_url", ""))
    monkeypatch.setattr(settings, "jwt_algorithms", over.get("algorithms", ["HS256"]))
    monkeypatch.setattr(settings, "jwt_issuer", over.get("issuer", ""))
    monkeypatch.setattr(settings, "jwt_audience", over.get("audience", ""))
    return JwtProvider()


def _token(claims: dict, secret=SECRET, alg="HS256", headers=None) -> str:
    return pyjwt.encode(claims, secret, algorithm=alg, headers=headers)


def test_jwt_valid_token(monkeypatch):
    p = _jwt_provider(monkeypatch)
    tok = _token({"sub": "usr_1", "exp": time.time() + 60, "role": "admin"})
    principal = asyncio.run(p.authenticate(FakeRequest({"authorization": f"Bearer {tok}"})))
    assert principal.id == "usr_1"
    assert principal.claims["role"] == "admin"


def test_jwt_expired_rejected(monkeypatch):
    p = _jwt_provider(monkeypatch)
    tok = _token({"sub": "u", "exp": time.time() - 120})  # beyond 30s leeway
    with pytest.raises(AuthError, match="invalid token"):
        asyncio.run(p.authenticate(FakeRequest({"authorization": f"Bearer {tok}"})))


def test_jwt_alg_pinning(monkeypatch):
    p = _jwt_provider(monkeypatch)  # allowlist: HS256 only
    tok = _token({"sub": "u", "exp": time.time() + 60}, alg="HS512")
    with pytest.raises(AuthError, match="not allowed"):
        asyncio.run(p.authenticate(FakeRequest({"authorization": f"Bearer {tok}"})))


def test_jwt_no_sub_rejected(monkeypatch):
    p = _jwt_provider(monkeypatch)
    tok = _token({"exp": time.time() + 60})
    with pytest.raises(AuthError, match="no sub"):
        asyncio.run(p.authenticate(FakeRequest({"authorization": f"Bearer {tok}"})))


def test_jwt_not_a_jwt_passes_to_next_provider(monkeypatch):
    p = _jwt_provider(monkeypatch)
    result = asyncio.run(p.authenticate(FakeRequest({"authorization": "Bearer not-a-jwt"})))
    assert result is None  # None = not my credential type; chain continues


def test_jwks_mode_requires_iss_aud(monkeypatch):
    with pytest.raises(RuntimeError, match="mandatory"):
        _jwt_provider(
            monkeypatch, secret="", jwks_url="https://issuer/jwks", algorithms=["RS256"]
        )


def test_jwks_url_must_be_https(monkeypatch):
    with pytest.raises(RuntimeError, match="https"):
        _jwt_provider(
            monkeypatch, secret="", jwks_url="http://issuer/jwks", algorithms=["RS256"],
            issuer="i", audience="a",
        )


def test_ws_token_from_subprotocol():
    from fastapi import WebSocket

    class FakeWs(WebSocket):
        def __init__(self):  # bypass starlette init; only headers needed
            self._h = {"sec-websocket-protocol": "bearer, tok123"}

        @property
        def headers(self):
            return self._h

    assert bearer_token(FakeWs()) == "tok123"


def test_api_key_generate_shape():
    from src.auth.providers.api_key_provider import generate_key

    plaintext, digest, prefix = generate_key()
    assert plaintext.startswith("sk_") and len(plaintext) > 40
    assert len(digest) == 64
    assert plaintext.startswith(prefix)


def test_api_key_negative_cache(monkeypatch):
    from src.auth.providers.api_key_provider import ApiKeyProvider

    p = ApiKeyProvider()
    lookups = 0

    async def fake_lookup(digest):
        nonlocal lookups
        lookups += 1
        return None

    monkeypatch.setattr(p, "_lookup", fake_lookup)

    async def flow():
        for _ in range(5):
            with pytest.raises(AuthError):
                await p.authenticate(FakeRequest({"x-api-key": "bogus"}))

    asyncio.run(flow())
    assert lookups == 1  # 4 of 5 served from the negative cache — no PG hammering


def test_chain_first_match_wins_and_dev_principal(monkeypatch):
    from src.auth import deps

    monkeypatch.setattr(settings, "auth_providers", ["jwt"])
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "jwt_jwks_url", "")
    monkeypatch.setattr(settings, "jwt_algorithms", ["HS256"])
    deps._instances.clear()

    auth = deps.Auth()
    tok = _token({"sub": "usr_9", "exp": time.time() + 60})
    principal = asyncio.run(auth.authenticate(FakeRequest({"authorization": f"Bearer {tok}"})))
    assert principal.id == "usr_9"

    # no credentials + dev principal set → dev principal
    monkeypatch.setattr(settings, "auth_dev_principal", "usr_dev")
    principal = asyncio.run(auth.authenticate(FakeRequest({})))
    assert principal.id == "usr_dev" and principal.claims == {"dev": True}

    # no credentials, no dev principal → 401
    monkeypatch.setattr(settings, "auth_dev_principal", "")
    with pytest.raises(AuthError, match="no credentials"):
        asyncio.run(auth.authenticate(FakeRequest({})))


def test_api_key_roundtrip_against_db(monkeypatch):
    from tests.test_db import _pg_reachable

    if not _pg_reachable():
        pytest.skip("postgres not reachable")

    from sqlalchemy import text

    from src.auth.providers.api_key_provider import ApiKeyProvider, generate_key

    plaintext, digest, prefix = generate_key()

    async def flow():
        from src.database.engine import get_engine

        await get_engine().dispose()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO api_key (key_hash, prefix, name, principal_id, revoked) "
                    "VALUES (:h, :p, 'test', 'usr_42', false)"
                ),
                {"h": digest, "p": prefix},
            )
        p = ApiKeyProvider()
        principal = await p.authenticate(FakeRequest({"x-api-key": plaintext}))
        async with get_engine().begin() as conn:
            await conn.execute(text("DELETE FROM api_key WHERE key_hash=:h"), {"h": digest})
        await get_engine().dispose()
        return principal

    principal = asyncio.run(flow())
    assert principal == Principal(id="usr_42", kind="api_key", claims={"key_name": "test"})
