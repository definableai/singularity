"""Service registrar (01) — strict `http_exposed` auto-registration.

Folder path → URL path (`services/users/` → `/api/v1/users`); `*Service` class
discovery; string entries `"post=create"` or dict escape hatch
`{"verb": "post", "path": "create", "status": 201}`.
"""

import inspect
import re
import time
from typing import Any

from fastapi import Depends, FastAPI, Request

from src.core.context import Context
from src.core.loader import load_package

VERBS = {"get", "post", "put", "delete", "patch", "ws"}
_ORPHAN_RE = re.compile(rf"^({'|'.join(VERBS)})_")

API_PREFIX = "/api/v1"


class RegistrarError(Exception):
    pass


def _parse_entry(entry: str | dict) -> dict[str, Any]:
    if isinstance(entry, dict):
        parsed = dict(entry)
        parsed.setdefault("path", "")
    else:
        verb, _, path = entry.partition("=")
        parsed = {"verb": verb, "path": path}
    if parsed["verb"] not in VERBS:
        raise RegistrarError(f"unknown verb in http_exposed entry {entry!r}")
    return parsed


def _method_name(verb: str, path: str) -> str:
    segments = [s for s in path.split("/") if s and not s.startswith("{")]
    return "_".join([verb, *segments]) if segments else verb


def _make_endpoint(service_cls: type, method_name: str, ctx: Context):
    fn = getattr(service_cls, method_name)
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "self"]
    # detect by annotation, not name — an unannotated `request=...` is a query param
    has_request = any(p.name == "request" and p.annotation is Request for p in params)

    qualname = f"{service_cls.__name__}.{method_name}"

    async def endpoint(**kwargs):
        from src.tracing import journey

        request: Request = kwargs["request"] if has_request else kwargs.pop("request")
        t0 = time.perf_counter()
        result = await fn(service_cls(ctx), **kwargs)
        # Commit BEFORE the response is sent — teardown-commit is silent data loss (02).
        session = getattr(request.state, "db_session", None)
        if session is not None:
            await session.commit()
            # submit-after-commit (04): deferred task submits fire only now, shielded —
            # the DB change is durable, so losing the submit silently is not allowed
            j = journey.current()
            for send in getattr(j, "singularity_pending_submits", []) if j else []:
                import asyncio

                await asyncio.shield(asyncio.to_thread(send))
        if (j := journey.current()) is not None:
            j.add_step("endpoint", qualname, duration_ms=(time.perf_counter() - t0) * 1000)
        return result

    extra = (
        []
        if has_request
        else [
            inspect.Parameter(
                "request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request
            )
        ]
    )
    endpoint.__signature__ = inspect.Signature(
        extra + params, return_annotation=sig.return_annotation
    )
    endpoint.__name__ = method_name
    endpoint.__doc__ = fn.__doc__
    return endpoint


def _make_ws_endpoint(service_cls: type, method_name: str, ctx: Context):
    fn = getattr(service_cls, method_name)
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "self"]

    async def endpoint(**kwargs):
        await fn(service_cls(ctx), **kwargs)

    endpoint.__signature__ = inspect.Signature(params)
    endpoint.__name__ = method_name
    return endpoint


def register_services(app: FastAPI, ctx: Context, package: str = "src.services") -> list[str]:
    """Walk service modules, validate strictly, mount routes. Returns route descriptions."""
    registered: list[str] = []
    for module in load_package(package):
        if not module.__name__.endswith(".service"):
            continue
        cls = next(
            (
                obj
                for name, obj in vars(module).items()
                if inspect.isclass(obj)
                and name.endswith("Service")
                and obj.__module__ == module.__name__
            ),
            None,
        )
        if cls is None:
            raise RegistrarError(f"{module.__name__}: no *Service class found")

        # services.users.service → /api/v1/users
        rel = module.__name__.removeprefix(package + ".").removesuffix(".service")
        base = f"{API_PREFIX}/{rel.replace('.', '/')}"
        tag = rel.split(".")[-1]

        deps = [Depends(cls.auth)] if getattr(cls, "auth", None) else []
        covered: set[str] = set()

        for raw in getattr(cls, "http_exposed", []):
            entry = _parse_entry(raw)
            verb, path = entry["verb"], entry["path"]
            method = _method_name(verb, path)
            covered.add(method)
            if not hasattr(cls, method):
                raise RegistrarError(
                    f"{cls.__name__}: http_exposed entry {raw!r} has no method {method!r}"
                )
            url = f"{base}/{path}" if path else base
            if verb == "ws":
                app.add_api_websocket_route(url, _make_ws_endpoint(cls, method, ctx))
            else:
                app.add_api_route(
                    url,
                    _make_endpoint(cls, method, ctx),
                    methods=[verb.upper()],
                    status_code=entry.get("status", 200),
                    dependencies=deps,
                    tags=[tag],
                    description=cls.__doc__ or "",
                )
            registered.append(f"{verb.upper()} {url} → {cls.__name__}.{method}")

        # {verb}_{path} method without an entry → startup warning, not silence.
        for name, member in inspect.getmembers(cls, inspect.isfunction):
            if _ORPHAN_RE.match(name) and name not in covered:
                from src.common.logger import log

                log.warning(f"{cls.__name__}.{name} looks routable but has no http_exposed entry")
    return registered
