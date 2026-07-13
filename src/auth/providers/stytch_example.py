"""BYO example (03): a working vendor adapter shape — copy this file to integrate any
vendor. Configure with AUTH_PROVIDERS=["src.auth.providers.stytch_example.StytchProvider"].

Stytch session JWTs are plain JWKS-signed JWTs, so the adapter is: point the hardened
JWT machinery at Stytch's JWKS, then map their claim layout onto Principal.
"""

from fastapi import Request, WebSocket

from src.auth.protocol import Principal
from src.auth.providers.jwt_provider import JwtProvider


class StytchProvider(JwtProvider):
    name = "stytch"

    # For a real integration set (or read from your own settings):
    #   JWT_JWKS_URL=https://api.stytch.com/v1/sessions/jwks/<project_id>
    #   JWT_ISSUER=stytch.com/<project_id>
    #   JWT_AUDIENCE=<project_id>
    #   JWT_ALGORITHMS=["RS256"]

    async def authenticate(self, request: Request | WebSocket) -> Principal | None:
        principal = await super().authenticate(request)
        if principal is None:
            return None
        # Stytch puts the session in a nested claim; surface what apps actually use.
        session = principal.claims.get("https://stytch.com/session", {})
        return Principal(
            id=principal.id,  # sub = stytch user id
            kind="user",
            claims={"session_id": session.get("id"), **principal.claims},
        )
