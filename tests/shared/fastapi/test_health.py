"""Tests for the shared health router factory.

``make_health_router(deps_callable=...)`` returns a FastAPI APIRouter
with two endpoints:

* ``GET /health`` — fast liveness, always 200, no DI.
* ``GET /health/deps`` — calls ``deps_callable()`` (sync OR async) which
  must return a ``dict[str, str]`` mapping dependency name -> status
  literal (``"ok"`` / anything else for unhealthy). Returns 200 when ALL
  values are ``"ok"``, else 503 with the same payload.

This contract is the union of the four products' existing patterns:

* BSGateway / BSupervisor return 503 when any dep is unreachable.
* BSNexus returns 200 even when a dep is down (ops dashboard usability).
  The new contract picks the BSGateway/BSupervisor behaviour because it
  matches load-balancer / k8s probe expectations.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.shared.fastapi.health import make_health_router


def _make_app(deps_callable=None) -> FastAPI:
    app = FastAPI()
    app.include_router(make_health_router(deps_callable=deps_callable))
    return app


class TestLivenessEndpoint:
    def test_liveness_returns_200(self) -> None:
        client = TestClient(_make_app())
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_liveness_does_not_call_deps_callable(self) -> None:
        called = {"n": 0}

        def deps() -> dict[str, str]:
            called["n"] += 1
            return {}

        client = TestClient(_make_app(deps))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert called["n"] == 0


class TestDepsEndpointSync:
    def test_all_ok_returns_200(self) -> None:
        def deps() -> dict[str, str]:
            return {"database": "ok", "redis": "ok"}

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 200
        assert resp.json() == {"database": "ok", "redis": "ok"}

    def test_one_unhealthy_returns_503(self) -> None:
        def deps() -> dict[str, str]:
            return {"database": "ok", "redis": "error: timeout"}

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 503
        assert resp.json()["redis"] == "error: timeout"

    def test_empty_deps_returns_200(self) -> None:
        # No dependencies registered = trivially healthy. Matches the
        # behaviour products expect when the callable returns {}.
        def deps() -> dict[str, str]:
            return {}

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_deps_callable_raising_returns_503_with_error(self) -> None:
        def deps() -> dict[str, str]:
            raise RuntimeError("kaboom")

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 503
        # Surface a deterministic error key so ops can scrape it.
        body = resp.json()
        assert "error" in body

    def test_deps_callable_returning_non_dict_returns_503(self) -> None:
        # A misbehaving deps_callable that returns the wrong type must
        # not crash the route — it surfaces as a 503 with the type error.
        def deps():
            return ["not", "a", "dict"]

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 503
        assert "error" in resp.json()


class TestDepsEndpointAsync:
    def test_async_deps_callable_supported(self) -> None:
        async def deps() -> dict[str, str]:
            return {"database": "ok"}

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 200
        assert resp.json() == {"database": "ok"}

    def test_async_deps_callable_unhealthy(self) -> None:
        async def deps() -> dict[str, str]:
            return {"database": "ok", "redis": "down"}

        client = TestClient(_make_app(deps))
        resp = client.get("/health/deps")
        assert resp.status_code == 503


class TestDepsCallableOptional:
    def test_no_deps_callable_returns_200_empty(self) -> None:
        # When products do not pass deps_callable (e.g. BSupervisor today
        # has only /api/health), /health/deps must still answer rather
        # than 404; trivially-healthy keeps probes simple.
        client = TestClient(_make_app())
        resp = client.get("/health/deps")
        assert resp.status_code == 200
        assert resp.json() == {}


class TestRouterPrefix:
    def test_default_router_prefix_is_empty(self) -> None:
        # Default mounts at /health and /health/deps. Products that
        # already serve under /api/health prefix the include_router call.
        app = FastAPI()
        app.include_router(make_health_router())
        paths = {route.path for route in app.routes}
        assert "/health" in paths
        assert "/health/deps" in paths

    def test_router_can_be_prefixed_by_caller(self) -> None:
        app = FastAPI()
        app.include_router(make_health_router(), prefix="/api")
        paths = {route.path for route in app.routes}
        assert "/api/health" in paths
        assert "/api/health/deps" in paths

    def test_factory_accepts_prefix_kwarg(self) -> None:
        # Phase A cleanup — products converged on the `/api/...` URL
        # convention, so the factory accepts `prefix` directly to save
        # callers the boilerplate of `include_router(..., prefix="/api")`.
        app = FastAPI()
        app.include_router(make_health_router(prefix="/api"))
        paths = {route.path for route in app.routes}
        assert "/api/health" in paths
        assert "/api/health/deps" in paths

    def test_factory_prefix_default_is_empty(self) -> None:
        # Default behaviour MUST stay backward-compatible — products that
        # don't pass `prefix` continue to mount at /health and /health/deps.
        app = FastAPI()
        app.include_router(make_health_router(prefix=""))
        paths = {route.path for route in app.routes}
        assert "/health" in paths
        assert "/health/deps" in paths

    def test_factory_prefix_with_deps_callable(self) -> None:
        # `prefix` must compose cleanly with `deps_callable` — products
        # use them together (BSGateway/BSNexus/BSage/BSupervisor all have
        # a deps probe under `/api/health/deps`).
        def deps() -> dict[str, str]:
            return {"db": "ok"}

        app = FastAPI()
        app.include_router(make_health_router(prefix="/api", deps_callable=deps))

        client = TestClient(app)
        liveness = client.get("/api/health")
        assert liveness.status_code == 200
        assert liveness.json() == {"status": "ok"}

        readiness = client.get("/api/health/deps")
        assert readiness.status_code == 200
        assert readiness.json() == {"db": "ok"}

        # And the un-prefixed paths must NOT be registered.
        assert client.get("/health").status_code == 404
