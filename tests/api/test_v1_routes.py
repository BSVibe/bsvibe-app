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
        "/api/v1/intents",
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


def test_intents_list_requires_auth() -> None:
    """Even the intents catalog requires a verified principal (all v1 routers)."""
    r = _client().get("/api/v1/intents")
    assert r.status_code == 401


def test_legacy_layer2_routes_are_gone() -> None:
    """Unified routing Lift 2 hard-deleted the Layer-2 model-routing surface.
    The /rules + /presets routes must NOT reappear in the OpenAPI spec."""
    paths = set(_client().get("/api/openapi.json").json()["paths"].keys())
    for gone in ("/api/v1/rules", "/api/v1/presets", "/api/v1/presets/{preset_name}/apply"):
        assert gone not in paths, f"{gone} should have been deleted"


def test_legacy_routing_rules_mcp_tools_are_gone() -> None:
    """The bsvibe_routing_rules_* MCP tools were hard-deleted; only the
    run-routing (bsvibe_run_routing_rules_*) surface survives."""
    from backend.mcp.api import ToolRegistry
    from backend.mcp.tools import register_all_tools

    reg = ToolRegistry()
    register_all_tools(reg)
    names = set(reg.names())
    assert not any(n.startswith("bsvibe_routing_rules_") for n in names)
    assert any(n.startswith("bsvibe_run_routing_rules_") for n in names)
