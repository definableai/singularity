"""Escape-hatch middleware discovery (01).

`src/middlewares/` ships empty. Contract: pure ASGI — class `Middleware` with
`__init__(self, app)` + `async __call__(scope, receive, send)`. Sorted filename order.
Subclassing BaseHTTPMiddleware is a boot error: ~1.8x throughput cost and breaks the
contextvar propagation 05/06 stand on.
"""

from starlette.middleware.base import BaseHTTPMiddleware

from src.core.loader import load_package


def discover_middlewares(package: str = "src.middlewares") -> list[type]:
    classes = []
    for module in sorted(load_package(package), key=lambda m: m.__name__):
        mw = vars(module).get("Middleware")
        if mw is None:
            continue
        if issubclass(mw, BaseHTTPMiddleware):
            raise TypeError(
                f"{module.__name__}.Middleware subclasses BaseHTTPMiddleware — "
                "write pure ASGI (__init__(self, app), __call__(scope, receive, send))"
            )
        classes.append(mw)
    return classes
