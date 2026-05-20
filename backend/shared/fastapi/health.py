"""Shared health router factory.

The four products converge on the same two endpoints:

* ``GET /health`` — liveness, always 200, no DI. Load-balancer probe.
* ``GET /health/deps`` — dependency-aware readiness. Returns 200 when
  every dependency reports ``"ok"`` (case-sensitive); otherwise 503 with
  the same map so ops dashboards can scrape the broken key.

Products inject a single ``deps_callable`` that returns a
``dict[str, str]`` of dependency-name -> status literal. Sync OR async
callables are accepted — the factory inspects ``asyncio.iscoroutine``
on the return value rather than ``inspect.iscoroutinefunction`` so
lambdas and partials work transparently.

Exceptions inside ``deps_callable`` do NOT crash the route — they are
logged and surfaced as ``{"error": "..."}`` with a 503 status so the
liveness probe stays cheap and the readiness probe stays honest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


DepsResult = dict[str, str]
DepsCallable = Callable[[], DepsResult] | Callable[[], Awaitable[DepsResult]]


async def _invoke_deps(deps_callable: DepsCallable) -> DepsResult:
    result: Any = deps_callable()
    if asyncio.iscoroutine(result):
        result = await result
    if not isinstance(result, dict):
        raise TypeError(f"deps_callable must return dict[str, str], got {type(result).__name__}")
    return result


def make_health_router(
    *,
    deps_callable: DepsCallable | None = None,
    prefix: str = "",
) -> APIRouter:
    """Build a FastAPI router that exposes ``/health`` and ``/health/deps``.

    When ``deps_callable`` is None, ``/health/deps`` returns 200 with an
    empty map — trivially healthy, so probes do not 404.

    The ``prefix`` is forwarded to ``APIRouter(prefix=prefix)`` so
    products that mount everything under ``/api`` can pass
    ``prefix="/api"`` instead of repeating
    ``include_router(..., prefix="/api")`` at every call site. Default
    ``""`` keeps the pre-existing ``/health`` mount points
    (backward-compatible).
    """

    router = APIRouter(prefix=prefix, tags=["health"])

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/deps")
    async def health_deps() -> JSONResponse:
        if deps_callable is None:
            return JSONResponse(status_code=200, content={})

        try:
            result = await _invoke_deps(deps_callable)
        except Exception as exc:  # noqa: BLE001 — surface every failure
            logger.error("health_deps_callable_failed", error=str(exc), exc_info=True)
            return JSONResponse(status_code=503, content={"error": str(exc)})

        all_ok = all(value == "ok" for value in result.values())
        status_code = 200 if all_ok else 503
        return JSONResponse(status_code=status_code, content=result)

    return router


__all__ = ["make_health_router", "DepsCallable", "DepsResult"]
