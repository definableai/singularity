"""Health check — the example service. Copy this shape for your first real one."""

from src.core.context import Context


class HealthzService:
    """Liveness with a framework twist: proves the registrar mounted your folder."""

    http_exposed = ["get"]

    def __init__(self, ctx: Context):
        self.ctx = ctx

    async def get(self) -> dict:
        return {"status": "ok", "environment": self.ctx.settings.environment}
