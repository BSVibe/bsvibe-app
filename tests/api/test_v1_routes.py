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


def test_chat_completions_validates_payload() -> None:
    # Missing messages → 422 before any auth / DB.
    r = _client().post("/api/v1/chat/completions", json={"model": "x"})
    assert r.status_code == 422


def test_chat_completions_accepts_metadata_passthrough() -> None:
    # Metadata accepted; full handler still raises (no auth wired here).
    r = _client().post(
        "/api/v1/chat/completions",
        json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"bsvibe_account_id": "11111111-1111-1111-1111-111111111111"},
        },
    )
    # 501 because chat dispatch path is still a stub at the HTTP layer
    # (LiteLLMHook construction happens above this handler in Bundle G).
    assert r.status_code == 501


def test_settings_is_unauthenticated() -> None:
    """Settings is a read-only operator view — no auth dependency."""
    r = _client().get("/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert "environment" in body
    assert "knowledge_vault_root" in body


def test_presets_list_is_unauthenticated() -> None:
    """Built-in preset templates don't require auth — they're catalog data."""
    r = _client().get("/api/v1/presets")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    names = {p["name"] for p in body}
    # Bundle 1.5e ships these four built-ins.
    assert {"coding-assistant", "customer-support", "translation-summary", "general"} <= names
