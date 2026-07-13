"""ctx.ws (01): in-process connection manager.

Single-replica contract, stated loudly: this manager only reaches connections on ITS OWN
replica. broadcast() warns once in prod. Scaling WS past one replica needs a Redis
pub/sub backplane — deliberately not shipped (see plan 01).
"""

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._warned = False

    async def connect(self, channel: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(channel, set()).add(ws)

    def disconnect(self, channel: str, ws: WebSocket) -> None:
        self._connections.get(channel, set()).discard(ws)

    async def broadcast(self, channel: str, message: dict) -> int:
        """→ how many connections received it — on THIS replica only."""
        from src.config.settings import settings

        if not self._warned and not settings.is_dev:
            from src.common.logger import log

            log.warning(
                "WebSocketManager.broadcast is single-replica: connections on other "
                "replicas will NOT receive this message (plan 01)"
            )
            self._warned = True
        dead = []
        conns = self._connections.get(channel, set())
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            conns.discard(ws)
        return len(conns)
