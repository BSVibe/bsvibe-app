"""Smoke + route-presence tests for /api/v1/* skeleton."""

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
        "/api/v1/skills",
        "/api/v1/decisions",
        "/api/v1/settings",
        "/api/v1/runs",
    ):
        assert route in paths, f"missing {route} in OpenAPI"


def test_chat_completions_returns_501() -> None:
    r = _client().post(
        "/api/v1/chat/completions",
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 501
    assert "not yet wired" in r.json()["detail"]


def test_chat_completions_rejects_invalid_payload() -> None:
    r = _client().post("/api/v1/chat/completions", json={"model": "x"})  # missing messages
    assert r.status_code == 422


def test_chat_completions_accepts_metadata_passthrough() -> None:
    r = _client().post(
        "/api/v1/chat/completions",
        json={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"bsvibe_account_id": "11111111-1111-1111-1111-111111111111"},
        },
    )
    # Pydantic accepts the metadata; dispatch returns 501 (not wired).
    assert r.status_code == 501


def test_workspaces_list_is_skeleton() -> None:
    r = _client().get("/api/v1/workspaces")
    assert r.status_code == 501


def test_skills_list_is_skeleton() -> None:
    r = _client().get("/api/v1/skills")
    assert r.status_code == 501


def test_runs_list_is_skeleton() -> None:
    r = _client().get("/api/v1/runs")
    assert r.status_code == 501
