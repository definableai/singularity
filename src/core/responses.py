"""The one JSON response (01).

FastAPI >= 0.139 deprecates its own ORJSONResponse: a handler with an annotated return is
serialized straight to JSON bytes via Pydantic, which is faster and needs no response
class at all — which is why create_app() sets no default_response_class (app.py).

That covers ordinary handlers. It does not cover a handler that must pick its own status
code (/readyz), an error envelope (core/errors.py), or the dashboard's hand-built payloads
(obs/dashboard.py) — those still return a Response object, and orjson still encodes it.
This is that Response. Same call shape as the class FastAPI deprecated.
"""

from typing import Any

import orjson
from starlette.responses import Response


class JSONResponse(Response):
    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content)
