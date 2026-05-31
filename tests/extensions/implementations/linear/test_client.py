"""Tests for LinearClient — the httpx GraphQL wrapper. httpx is mocked via
respx; no real Linear calls. (rule python-testing: never call real APIs in
tests.)"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.extensions.implementations.linear.client import LinearApiError, LinearClient

API = "https://api.linear.app"
GQL = f"{API}/graphql"


@pytest.fixture
def client() -> LinearClient:
    return LinearClient("lin_api_secret", base_url=API)


class TestCreateIssue:
    @respx.mock
    async def test_create_issue_posts_and_parses_issue(self, client):
        route = respx.post(GQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "issueCreate": {
                            "success": True,
                            "issue": {
                                "id": "iss-1",
                                "identifier": "ENG-12",
                                "url": "https://linear.app/x/issue/ENG-12",
                            },
                        }
                    }
                },
            )
        )
        issue = await client.create_issue(team_id="team-1", title="Spec", description="details")
        assert issue["id"] == "iss-1"
        assert issue["identifier"] == "ENG-12"
        assert issue["url"] == "https://linear.app/x/issue/ENG-12"
        assert route.called
        sent = route.calls.last.request
        # raw api key in Authorization (NOT Bearer-prefixed)
        assert sent.headers["Authorization"] == "lin_api_secret"
        assert sent.headers["Content-Type"] == "application/json"
        # GraphQL variables carry the input, not string interpolation
        assert b'"variables"' in sent.content
        assert b"team-1" in sent.content
        assert b"Spec" in sent.content
        assert b"details" in sent.content

    @respx.mock
    async def test_create_issue_raises_on_graphql_errors_envelope(self, client):
        # HTTP 200 but a GraphQL-level errors body → NOT a success.
        respx.post(GQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "errors": [
                        {"message": "Argument Validation Error", "extensions": {"code": "INVALID"}}
                    ]
                },
            )
        )
        with pytest.raises(LinearApiError, match="Argument Validation Error"):
            await client.create_issue(team_id="team-1", title="T")

    @respx.mock
    async def test_create_issue_raises_on_non_2xx(self, client):
        respx.post(GQL).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_issue(team_id="team-1", title="T")

    @respx.mock
    async def test_create_issue_raises_when_no_issue_id(self, client):
        respx.post(GQL).mock(
            return_value=httpx.Response(
                200, json={"data": {"issueCreate": {"success": False, "issue": None}}}
            )
        )
        with pytest.raises(LinearApiError, match="no issue id"):
            await client.create_issue(team_id="team-1", title="T")


class TestArchiveIssue:
    @respx.mock
    async def test_archive_issue_posts_mutation(self, client):
        route = respx.post(GQL).mock(
            return_value=httpx.Response(200, json={"data": {"issueArchive": {"success": True}}})
        )
        data = await client.archive_issue("iss-1")
        assert data["issueArchive"]["success"] is True
        assert b"issueArchive" in route.calls.last.request.content
        assert b"iss-1" in route.calls.last.request.content

    @respx.mock
    async def test_archive_issue_raises_linear_api_error_with_code(self, client):
        respx.post(GQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "errors": [
                        {"message": "Entity not found", "extensions": {"code": "entityNotFound"}}
                    ]
                },
            )
        )
        with pytest.raises(LinearApiError) as excinfo:
            await client.archive_issue("iss-gone")
        assert excinfo.value.errors[0]["extensions"]["code"] == "entityNotFound"


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.post(GQL).mock(
            return_value=httpx.Response(
                200, json={"data": {"issueCreate": {"issue": {"id": "i1"}}}}
            )
        )
        async with httpx.AsyncClient() as injected:
            lc = LinearClient("tok", base_url=API, client=injected)
            issue = await lc.create_issue(team_id="t1", title="T")
        assert issue["id"] == "i1"
