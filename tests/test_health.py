"""Acceptance #3 — /api/health returns 200 with status, version, git_sha."""

from __future__ import annotations

import httpx
import pytest

from backend.api.main import create_app


@pytest.mark.asyncio
async def test_health_returns_200_with_required_fields() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]
    assert isinstance(body["git_sha"], str) and body["git_sha"]
