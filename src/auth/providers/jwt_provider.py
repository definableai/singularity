"""JWT provider (03). Hardening is spec, not option:
- secret mode → HS* only; JWKS mode → asymmetric allowlist; header alg outside the
  allowlist is rejected BEFORE signature verification (kills alg=none / HS-with-RSA-key)
- iss + aud mandatory in JWKS mode (boot error); JWKS URL must be https
- JWKS cache: TTL 15m, unknown-kid refresh capped 1/60s, 10s negative cache
"""

import time
from typing import Any

import jwt as pyjwt
from fastapi import Request, WebSocket

from src.auth.protocol import AuthError, Principal, bearer_token
from src.config.settings import settings

# Constants
LEEWAY_S = 30
JWKS_TTL_S = 900
JWKS_REFRESH_MIN_INTERVAL_S = 60
JWKS_NEGATIVE_CACHE_S = 10
HS_ALGS = {"HS256", "HS384", "HS512"}


class JwtProvider:
    name = "jwt"

    def __init__(self) -> None:
        self.secret = settings.jwt_secret
        self.jwks_url = settings.jwt_jwks_url
        if self.jwks_url:
            if not self.jwks_url.startswith("https://"):
                raise RuntimeError("JWT_JWKS_URL must be https")
            if not (settings.jwt_issuer and settings.jwt_audience):
                raise RuntimeError("JWT_ISSUER and JWT_AUDIENCE are mandatory in JWKS mode")
            self.algorithms = [a for a in settings.jwt_algorithms if a not in HS_ALGS]
            if not self.algorithms:
                raise RuntimeError("JWKS mode needs asymmetric algorithms (e.g. RS256, ES256)")
        elif self.secret:
            self.algorithms = [a for a in settings.jwt_algorithms if a in HS_ALGS] or ["HS256"]
        else:
            raise RuntimeError("jwt provider needs JWT_SECRET or JWT_JWKS_URL")
        self._keys: dict[str, Any] = {}
        self._fetched_at = 0.0
        self._last_refresh = 0.0
        self._bad_kids: dict[str, float] = {}

    async def authenticate(self, request: Request | WebSocket) -> Principal | None:
        token = bearer_token(request)
        if token is None:
            return None
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.InvalidTokenError:
            return None  # not a JWT — maybe another provider's bearer credential
        if header.get("alg") not in self.algorithms:
            raise AuthError(f"algorithm {header.get('alg')!r} not allowed")

        key = self.secret if self.secret else await self._jwks_key(header.get("kid"))
        try:
            claims = pyjwt.decode(
                token,
                key,
                algorithms=self.algorithms,
                leeway=LEEWAY_S,
                issuer=settings.jwt_issuer or None,
                audience=settings.jwt_audience or None,
                options={
                    "verify_iss": bool(settings.jwt_issuer),
                    "verify_aud": bool(settings.jwt_audience),
                },
            )
        except pyjwt.InvalidTokenError as e:
            raise AuthError(f"invalid token: {type(e).__name__}") from None
        sub = claims.get("sub")
        if not sub:
            raise AuthError("token has no sub")
        return Principal(id=str(sub), kind="user", claims=claims)

    async def _jwks_key(self, kid: str | None):
        if kid is None:
            raise AuthError("token has no kid")
        now = time.monotonic()
        if now - self._bad_kids.get(kid, -1e9) < JWKS_NEGATIVE_CACHE_S:
            raise AuthError("unknown signing key")
        stale = now - self._fetched_at > JWKS_TTL_S
        if (
            kid not in self._keys or stale
        ) and now - self._last_refresh > JWKS_REFRESH_MIN_INTERVAL_S:
            await self._refresh()
        if kid not in self._keys:
            self._bad_kids[kid] = time.monotonic()
            raise AuthError("unknown signing key")
        return self._keys[kid]

    async def _refresh(self) -> None:
        import httpx

        self._last_refresh = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                data = (await client.get(self.jwks_url)).raise_for_status().json()
            self._keys = {k["kid"]: pyjwt.PyJWK(k).key for k in data.get("keys", []) if "kid" in k}
            self._fetched_at = time.monotonic()
        except Exception:
            if not self._keys:
                raise AuthError("signing keys unavailable") from None
            # warm cache + fetch failure → serve from cache and log (03)
            from src.common.logger import log

            log.warning("JWKS refresh failed — serving cached keys")
