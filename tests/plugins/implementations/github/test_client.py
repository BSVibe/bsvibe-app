"""Tests for GithubClient — the httpx wrapper. httpx is mocked via respx;
no real GitHub calls. (rule python-testing: never call real APIs in tests.)"""

from __future__ import annotations

import httpx
import pytest
import respx

from backend.plugins.implementations.github.client import GithubClient

API = "https://api.github.com"


@pytest.fixture
def client() -> GithubClient:
    return GithubClient("tok-123", base_url=API)


class TestOpenPr:
    @respx.mock
    async def test_open_pr_posts_and_returns_json(self, client):
        route = respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 15, "html_url": f"{API.replace('api.', '')}/o/r/pull/15"}
            )
        )
        data = await client.open_pr("o", "r", head="feat", base="main", title="T", body="B")
        assert data["number"] == 15
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer tok-123"
        assert b'"head"' in sent.content and b'"feat"' in sent.content

    @respx.mock
    async def test_open_pr_raises_on_error(self, client):
        respx.post(f"{API}/repos/o/r/pulls").mock(
            return_value=httpx.Response(422, json={"message": "bad"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.open_pr("o", "r", head="feat", base="main", title="T", body="B")


class TestUpdateAndGetPr:
    @respx.mock
    async def test_update_pr_patches(self, client):
        route = respx.patch(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "title": "New"})
        )
        data = await client.update_pr("o", "r", 15, title="New")
        assert data["title"] == "New"
        assert route.calls.last.request.method == "PATCH"

    @respx.mock
    async def test_get_pr(self, client):
        respx.get(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "open"})
        )
        data = await client.get_pr("o", "r", 15)
        assert data["state"] == "open"

    @respx.mock
    async def test_close_pr_sets_state_closed(self, client):
        route = respx.patch(f"{API}/repos/o/r/pulls/15").mock(
            return_value=httpx.Response(200, json={"number": 15, "state": "closed"})
        )
        await client.close_pr("o", "r", 15)
        assert b'"closed"' in route.calls.last.request.content


class TestComments:
    @respx.mock
    async def test_post_comment(self, client):
        route = respx.post(f"{API}/repos/o/r/issues/7/comments").mock(
            return_value=httpx.Response(201, json={"id": 99, "html_url": "u"})
        )
        data = await client.post_comment("o", "r", 7, "hello")
        assert data["id"] == 99
        assert b"hello" in route.calls.last.request.content

    @respx.mock
    async def test_delete_comment_returns_status(self, client):
        respx.delete(f"{API}/repos/o/r/issues/comments/99").mock(return_value=httpx.Response(204))
        status = await client.delete_comment("o", "r", 99)
        assert status == 204

    @respx.mock
    async def test_delete_comment_404_is_returned_not_raised(self, client):
        respx.delete(f"{API}/repos/o/r/issues/comments/99").mock(return_value=httpx.Response(404))
        status = await client.delete_comment("o", "r", 99)
        assert status == 404


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.get(f"{API}/repos/o/r/pulls/1").mock(
            return_value=httpx.Response(200, json={"number": 1, "state": "open"})
        )
        async with httpx.AsyncClient() as injected:
            gh = GithubClient("tok", base_url=API, client=injected)
            data = await gh.get_pr("o", "r", 1)
        assert data["number"] == 1
