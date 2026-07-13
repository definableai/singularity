"""Context — the one framework object app code receives (01).

Exhaustive member registry: settings, cache, http, ws, lock.
"""

from src.config.settings import Settings


class Context:
    def __init__(self, settings: Settings):
        from src.common.cache import Cache
        from src.common.lock import LockManager
        from src.common.websocket import WebSocketManager

        self.settings = settings
        self.cache = Cache("singularity", settings.environment)
        self.lock = LockManager()
        self.ws = WebSocketManager()

    @property
    def http(self):
        from src.common.http import get_http

        return get_http()
