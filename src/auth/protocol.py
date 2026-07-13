"""Auth protocol (03): Principal + AuthProvider. No RBAC — claims is the extension point."""

from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import Request, WebSocket
from pydantic import BaseModel

from src.core.errors import AppError


class Principal(BaseModel):
    id: str
    kind: Literal["user", "api_key", "service"]
    claims: dict[str, Any] = {}


class AuthError(AppError):
    """Authentication failed."""

    code = "auth_failed"
    status = 401


@runtime_checkable
class AuthProvider(Protocol):
    name: str

    async def authenticate(self, request: Request | WebSocket) -> Principal | None:
        """Principal, None (not my credential type), or raise AuthError (mine, invalid)."""
        ...


def bearer_token(request: Request | WebSocket) -> str | None:
    """HTTP: Authorization header. WS: Sec-WebSocket-Protocol pair ("bearer", <token>) —
    never the query string, which lands in access logs (03)."""
    if isinstance(request, WebSocket):
        protocols = request.headers.get("sec-websocket-protocol", "")
        parts = [p.strip() for p in protocols.split(",")]
        if len(parts) >= 2 and parts[0] == "bearer":
            return parts[1]
        return None
    auth = request.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else None
