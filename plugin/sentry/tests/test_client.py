"""Tests for SentryClient — the httpx wrapper. httpx is mocked via respx;
no real Sentry calls. (rule python-testing: never call real APIs in tests.)

Sentry uses ordinary REST semantics — a non-2xx response is a hard failure
mapped to :class:`SentryApiError` (status code preserved)."""

from __future__ import annotations

import httpx
import pytest
import respx

from plugin.sentry.client import SentryApiError, SentryClient

API = "https://sentry.io/api/0"


@pytest.fixture
def client() -> SentryClient:
    return SentryClient("sntrys_tok-123", base_url=API)


class TestUpdateIssueStatus:
    @respx.mock
    async def test_resolve_returns_json(self, client):
        route = respx.put(f"{API}/issues/100001/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "100001",
                    "status": "resolved",
                    "permalink": "https://sentry.io/org/proj/issues/100001/",
                },
            )
        )
        data = await client.update_issue_status("100001", "resolved")
        assert data["status"] == "resolved"
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer sntrys_tok-123"
        assert b'"status"' in sent.content and b"resolved" in sent.content

    @respx.mock
    async def test_unresolve_returns_json(self, client):
        respx.put(f"{API}/issues/100001/").mock(
            return_value=httpx.Response(200, json={"id": "100001", "status": "unresolved"})
        )
        data = await client.update_issue_status("100001", "unresolved")
        assert data["status"] == "unresolved"

    @respx.mock
    async def test_non_2xx_raises_sentry_api_error(self, client):
        respx.put(f"{API}/issues/100001/").mock(return_value=httpx.Response(403, text="forbidden"))
        with pytest.raises(SentryApiError) as excinfo:
            await client.update_issue_status("100001", "resolved")
        assert excinfo.value.status_code == 403

    @respx.mock
    async def test_404_raises_with_status_code(self, client):
        # 404 still raises here; the compensate handler decides idempotency.
        respx.put(f"{API}/issues/999/").mock(return_value=httpx.Response(404, text="not found"))
        with pytest.raises(SentryApiError) as excinfo:
            await client.update_issue_status("999", "unresolved")
        assert excinfo.value.status_code == 404


class TestListProjectIssues:
    @respx.mock
    async def test_list_project_issues_returns_list(self, client):
        respx.get(f"{API}/projects/org/proj/issues/").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "1", "title": "x", "status": "unresolved"},
                ],
            )
        )
        rows = await client.list_project_issues("org", "proj")
        assert len(rows) == 1
        assert rows[0]["id"] == "1"

    @respx.mock
    async def test_list_project_issues_carries_query_and_limit(self, client):
        route = respx.get(f"{API}/projects/org/proj/issues/").mock(
            return_value=httpx.Response(200, json=[])
        )
        await client.list_project_issues(
            "org", "proj", query="is:unresolved error.type:KeyError", per_page=5
        )
        url = str(route.calls[0].request.url)
        assert "query=" in url
        assert "KeyError" in url
        assert "limit=5" in url

    @respx.mock
    async def test_list_project_issues_non_2xx_raises(self, client):
        respx.get(f"{API}/projects/org/proj/issues/").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        with pytest.raises(SentryApiError) as excinfo:
            await client.list_project_issues("org", "proj")
        assert excinfo.value.status_code == 403


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.put(f"{API}/issues/1/").mock(
            return_value=httpx.Response(200, json={"id": "1", "status": "resolved"})
        )
        async with httpx.AsyncClient() as injected:
            sc = SentryClient("tok", base_url=API, client=injected)
            data = await sc.update_issue_status("1", "resolved")
        assert data["status"] == "resolved"
