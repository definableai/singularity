"""AppError hierarchy + the one response envelope: {error: {code, message, request_id}}."""

import traceback

from fastapi import FastAPI, Request

from src.core.asgi import request_id_var
from src.core.responses import JSONResponse

_codes: dict[str, type] = {}


class AppError(Exception):
    code = "app_error"
    status = 400

    def __init__(self, message: str | None = None):
        self.message = message or self.__doc__ or self.code
        super().__init__(self.message)

    def __init_subclass__(cls):
        # Stable error codes are a registry — duplicates are a boot error (01).
        if "code" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must declare a 'code'")
        if cls.code in _codes:
            raise TypeError(f"duplicate error code {cls.code!r}: {cls.__name__} vs {_codes[cls.code].__name__}")
        _codes[cls.code] = cls


class NotFoundError(AppError):
    """Resource not found."""

    code = "not_found"
    status = 404


def envelope(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id_var.get("")}},
        status_code=status,
    )


def register_handlers(app: FastAPI, dev: bool) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        # Handled errors never reach CoreLayer's except — record here (no traceback:
        # an AppError is a business outcome, not a defect).
        from src.tracing import journey

        if (j := journey.current()) is not None:
            j.add_step("exception", type(exc).__name__, code=exc.code, handled=True)
        return envelope(exc.code, exc.message, exc.status)

    try:
        from sqlalchemy.exc import InterfaceError, OperationalError, TimeoutError as PoolTimeout

        @app.exception_handler(PoolTimeout)
        @app.exception_handler(OperationalError)
        @app.exception_handler(InterfaceError)
        async def _db_unavailable(request: Request, exc: Exception):
            # Pool exhaustion / mid-request failover → fast 503, stable code (02).
            from src.common.logger import log

            log.error(f"db unavailable: {type(exc).__name__}")
            return envelope("db_unavailable", "database unavailable", 503)
    except ImportError:
        pass

    @app.exception_handler(Exception)
    async def _catch_all(request: Request, exc: Exception):
        # Journey recording happens in CoreLayer (the exception passes through it after
        # this handler's response is built); this handler only shapes the envelope.
        from src.common.logger import log

        log.exception(f"unhandled: {exc!r}")
        msg = traceback.format_exc() if dev else "internal error"
        return envelope("internal", msg, 500)
