"""Tests for NotionClient — the httpx wrapper. httpx is mocked via respx;
no real Notion calls. (rule python-testing: never call real APIs in tests.)"""

from __future__ import annotations

import httpx
import pytest
import respx

from plugin.notion.client import NotionClient

API = "https://api.notion.com"


@pytest.fixture
def client() -> NotionClient:
    return NotionClient("secret-tok", base_url=API)


class TestCreatePage:
    @respx.mock
    async def test_create_page_posts_and_returns_json(self, client):
        route = respx.post(f"{API}/v1/pages").mock(
            return_value=httpx.Response(
                200, json={"id": "page-1", "url": "https://notion.so/page-1"}
            )
        )
        data = await client.create_page(parent_page_id="par-1", title="T", body="line one")
        assert data["id"] == "page-1"
        assert route.called
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == "Bearer secret-tok"
        assert sent.headers["Notion-Version"] == "2022-06-28"
        assert b'"page_id"' in sent.content and b"par-1" in sent.content
        assert b"line one" in sent.content  # body became a paragraph block

    @respx.mock
    async def test_create_page_without_body_omits_children(self, client):
        route = respx.post(f"{API}/v1/pages").mock(
            return_value=httpx.Response(200, json={"id": "p", "url": "u"})
        )
        await client.create_page(parent_page_id="par-1", title="T")
        assert b'"children"' not in route.calls.last.request.content

    @respx.mock
    async def test_create_page_raises_on_error(self, client):
        respx.post(f"{API}/v1/pages").mock(
            return_value=httpx.Response(400, json={"message": "bad"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_page(parent_page_id="par-1", title="T")


class TestGetAndArchive:
    @respx.mock
    async def test_get_page(self, client):
        respx.get(f"{API}/v1/pages/page-1").mock(
            return_value=httpx.Response(200, json={"id": "page-1", "archived": False})
        )
        data = await client.get_page("page-1")
        assert data["archived"] is False

    @respx.mock
    async def test_archive_page_patches_archived_true(self, client):
        route = respx.patch(f"{API}/v1/pages/page-1").mock(
            return_value=httpx.Response(200, json={"id": "page-1", "archived": True})
        )
        status = await client.archive_page("page-1")
        assert status == 200
        assert b'"archived"' in route.calls.last.request.content
        assert b"true" in route.calls.last.request.content

    @respx.mock
    async def test_archive_page_404_is_returned_not_raised(self, client):
        respx.patch(f"{API}/v1/pages/page-1").mock(return_value=httpx.Response(404))
        status = await client.archive_page("page-1")
        assert status == 404

    @respx.mock
    async def test_archive_page_raises_on_other_error(self, client):
        respx.patch(f"{API}/v1/pages/page-1").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.archive_page("page-1")


class TestAppendBlock:
    @respx.mock
    async def test_append_block(self, client):
        route = respx.patch(f"{API}/v1/blocks/page-1/children").mock(
            return_value=httpx.Response(200, json={"object": "list"})
        )
        data = await client.append_block("page-1", "hello world")
        assert data["object"] == "list"
        assert b"hello world" in route.calls.last.request.content


class TestSearchPages:
    @respx.mock
    async def test_search_pages_filters_to_pages_only(self, client):
        route = respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [{"object": "page", "id": "p-1"}],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        pages = [p async for p in client.search_pages()]
        assert pages == [{"object": "page", "id": "p-1"}]
        sent = route.calls.last.request
        # MUST filter to pages (the spec gotcha — search mixes pages + databases).
        # httpx serialises without whitespace; assert against the wire form.
        assert b'"property":"object"' in sent.content
        assert b'"value":"page"' in sent.content

    @respx.mock
    async def test_search_pages_paginates_via_next_cursor(self, client):
        responses = [
            httpx.Response(
                200,
                json={
                    "results": [{"object": "page", "id": "p-1"}],
                    "has_more": True,
                    "next_cursor": "cur-2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "results": [{"object": "page", "id": "p-2"}],
                    "has_more": False,
                    "next_cursor": None,
                },
            ),
        ]
        route = respx.post(f"{API}/v1/search").mock(side_effect=responses)
        pages = [p async for p in client.search_pages()]
        ids = [p["id"] for p in pages]
        assert ids == ["p-1", "p-2"]
        assert route.call_count == 2
        # Second request body MUST include start_cursor=cur-2.
        second_body = route.calls[1].request.content
        assert b"cur-2" in second_body

    @respx.mock
    async def test_search_pages_401_raises(self, client):
        respx.post(f"{API}/v1/search").mock(
            return_value=httpx.Response(401, json={"message": "bad token"})
        )
        with pytest.raises(httpx.HTTPStatusError):
            [_ async for _ in client.search_pages()]


class TestQueryDatabase:
    @respx.mock
    async def test_query_database_yields_pages(self, client):
        respx.post(f"{API}/v1/databases/db-1/query").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [{"object": "page", "id": "p-a"}],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        pages = [p async for p in client.query_database("db-1")]
        assert pages == [{"object": "page", "id": "p-a"}]

    @respx.mock
    async def test_query_database_paginates(self, client):
        respx.post(f"{API}/v1/databases/db-1/query").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "results": [{"id": "p-a"}],
                        "has_more": True,
                        "next_cursor": "n1",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "results": [{"id": "p-b"}],
                        "has_more": False,
                        "next_cursor": None,
                    },
                ),
            ]
        )
        pages = [p async for p in client.query_database("db-1")]
        assert [p["id"] for p in pages] == ["p-a", "p-b"]


class TestListBlockChildren:
    @respx.mock
    async def test_list_block_children_yields_blocks(self, client):
        respx.get(f"{API}/v1/blocks/blk/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"object": "block", "id": "b1", "type": "paragraph"},
                        {"object": "block", "id": "b2", "type": "paragraph"},
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
        )
        blocks = [b async for b in client.list_block_children("blk")]
        assert [b["id"] for b in blocks] == ["b1", "b2"]

    @respx.mock
    async def test_list_block_children_paginates(self, client):
        respx.get(f"{API}/v1/blocks/blk/children").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "results": [{"id": "b1"}],
                        "has_more": True,
                        "next_cursor": "n",
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "results": [{"id": "b2"}],
                        "has_more": False,
                        "next_cursor": None,
                    },
                ),
            ]
        )
        blocks = [b async for b in client.list_block_children("blk")]
        assert [b["id"] for b in blocks] == ["b1", "b2"]


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.get(f"{API}/v1/pages/p1").mock(return_value=httpx.Response(200, json={"id": "p1"}))
        async with httpx.AsyncClient() as injected:
            nc = NotionClient("tok", base_url=API, client=injected)
            data = await nc.get_page("p1")
        assert data["id"] == "p1"

    @respx.mock
    async def test_custom_notion_version_header(self):
        route = respx.get(f"{API}/v1/pages/p1").mock(
            return_value=httpx.Response(200, json={"id": "p1"})
        )
        nc = NotionClient("tok", base_url=API, notion_version="2099-01-01")
        await nc.get_page("p1")
        assert route.calls.last.request.headers["Notion-Version"] == "2099-01-01"
