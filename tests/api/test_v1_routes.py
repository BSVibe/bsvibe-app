"""Route-presence + payload-validation smoke for /api/v1/*.

These tests run without DB / auth — they verify the route surface is
declared (OpenAPI) and that Pydantic validation fires before any handler
body runs. End-to-end tests against a real DB live in the
test_v1_*_routes / test_v1_accounts_decisions / test_v1_skills modules.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_openapi_advertises_all_v1_routes() -> None:
    spec = _client().get("/api/openapi.json").json()
    paths = set(spec["paths"].keys())
    for route in (
        "/api/v1/chat/completions",
        "/api/v1/workspaces",
        "/api/v1/products",
        "/api/v1/accounts",
        "/api/v1/rules",
        "/api/v1/intents",
        "/api/v1/presets",
        "/api/v1/presets/{preset_name}/apply",
        "/api/v1/skills",
        "/api/v1/decisions",
        "/api/v1/settings",
        "/api/v1/runs",
    ):
        assert route in paths, f"missing {route} in OpenAPI"


def test_chat_completions_requires_auth() -> None:
    # The v1 router-level auth dependency fires before Pydantic validation;
    # an unauthenticated request is rejected with 401.
    r = _client().post(
        "/api/v1/chat/completions",
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_settings_requires_auth() -> None:
    """Settings exposes deployment config — gated behind auth (all v1 routers)."""
    r = _client().get("/api/v1/settings")
    assert r.status_code == 401


def test_presets_list_requires_auth() -> None:
    """Even the preset catalog requires a verified principal (all v1 routers)."""
    r = _client().get("/api/v1/presets")
    assert r.status_code == 401
