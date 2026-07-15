"""Auth() dependency (03): provider chain from settings, first match wins.

user: Principal = Depends(Auth())            # full chain
user: Principal = Depends(Auth("api_key"))   # pin one provider
"""

import importlib
import time
from contextvars import ContextVar

from fastapi import Request, WebSocket

from src.auth.protocol import AuthError, AuthProvider, Principal
from src.config.settings import settings

principal_id_var: ContextVar[str] = ContextVar("principal_id", default="")

_SHIPPED = {
    "jwt": "src.auth.providers.jwt_provider.JwtProvider",
    "api_key": "src.auth.providers.api_key_provider.ApiKeyProvider",
}

_instances: dict[str, AuthProvider] = {}


def _provider(name: str) -> AuthProvider:
    if name not in _instances:
        dotted = _SHIPPED.get(name, name)  # shipped name or dotted path to a BYO class
        module, _, cls_name = dotted.rpartition(".")
        _instances[name] = getattr(importlib.import_module(module), cls_name)()
    return _instances[name]


def _dev_principal() -> Principal | None:
    if settings.auth_dev_principal and settings.is_dev:
        return Principal(id=settings.auth_dev_principal, kind="user", claims={"dev": True})
    return None


class Auth:
    def __init__(self, pin: str | None = None):
        self.pin = pin

    async def __call__(self, request: Request) -> Principal:
        return await self.authenticate(request)

    async def authenticate(self, request: Request | WebSocket) -> Principal:
        from src.tracing import journey

        # chain resolved per call, not at route-build: settings-driven and overridable
        names = [self.pin] if self.pin else settings.auth_providers
        for name in names:
            provider = _provider(name)
            t0 = time.perf_counter()
            principal = await provider.authenticate(
                request
            )  # AuthError propagates: its credential, invalid
            if principal is not None:
                principal_id_var.set(principal.id)
                if (j := journey.current()) is not None:
                    # provider tried, principal kind, duration — never the credential (03)
                    j.add_step(
                        "dependency",
                        f"auth:{name}",
                        duration_ms=(time.perf_counter() - t0) * 1000,
                        kind_matched=principal.kind,
                    )
                return principal
        if (dev := _dev_principal()) is not None:
            principal_id_var.set(dev.id)
            return dev
        raise AuthError("no credentials")
