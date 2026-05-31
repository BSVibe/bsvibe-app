"""Tests for TrelloClient — the httpx REST wrapper. httpx is mocked via
respx; no real Trello calls. (rule python-testing: never call real APIs in
tests.)"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from backend.extensions.implementations.trello.client import TrelloApiError, TrelloClient

API = "https://api.trello.com"
CARDS = f"{API}/1/cards"


@pytest.fixture
def client() -> TrelloClient:
    return TrelloClient("key-abc", "tok-xyz", base_url=API)


def _query(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(str(request.url)).query)


class TestCreateCard:
    @respx.mock
    async def test_create_card_posts_and_parses_card(self, client):
        route = respx.post(CARDS).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "card-1",
                    "name": "Spec",
                    "url": "https://trello.com/c/abc/1-spec",
                    "shortUrl": "https://trello.com/c/abc",
                },
            )
        )
        card = await client.create_card(list_id="list-1", name="Spec", desc="details")
        assert card["id"] == "card-1"
        assert card["shortUrl"] == "https://trello.com/c/abc"
        assert route.called
        # Auth + payload travel as query params (NOT a Bearer header / JSON body).
        q = _query(route.calls.last.request)
        assert q["key"] == ["key-abc"]
        assert q["token"] == ["tok-xyz"]
        assert q["idList"] == ["list-1"]
        assert q["name"] == ["Spec"]
        assert q["desc"] == ["details"]
        assert "authorization" not in {k.lower() for k in route.calls.last.request.headers}

    @respx.mock
    async def test_create_card_raises_on_non_2xx(self, client):
        respx.post(CARDS).mock(return_value=httpx.Response(401, text="invalid token"))
        with pytest.raises(TrelloApiError) as excinfo:
            await client.create_card(list_id="list-1", name="T")
        assert excinfo.value.status_code == 401

    @respx.mock
    async def test_create_card_raises_when_no_id(self, client):
        respx.post(CARDS).mock(return_value=httpx.Response(200, json={"name": "T"}))
        with pytest.raises(TrelloApiError, match="no id"):
            await client.create_card(list_id="list-1", name="T")


class TestArchiveCard:
    @respx.mock
    async def test_archive_card_puts_closed_true(self, client):
        route = respx.put(f"{CARDS}/card-1").mock(
            return_value=httpx.Response(200, json={"id": "card-1", "closed": True})
        )
        status = await client.archive_card("card-1")
        assert status == 200
        q = _query(route.calls.last.request)
        assert q["closed"] == ["true"]
        assert q["key"] == ["key-abc"]
        assert q["token"] == ["tok-xyz"]

    @respx.mock
    async def test_archive_card_returns_404_without_raising(self, client):
        respx.put(f"{CARDS}/gone").mock(return_value=httpx.Response(404, text="not found"))
        status = await client.archive_card("gone")
        assert status == 404

    @respx.mock
    async def test_archive_card_raises_on_other_error(self, client):
        respx.put(f"{CARDS}/card-1").mock(return_value=httpx.Response(429, text="rate limited"))
        with pytest.raises(TrelloApiError) as excinfo:
            await client.archive_card("card-1")
        assert excinfo.value.status_code == 429


class TestInjectedClient:
    @respx.mock
    async def test_uses_injected_async_client(self):
        respx.post(CARDS).mock(return_value=httpx.Response(200, json={"id": "c1"}))
        async with httpx.AsyncClient() as injected:
            tc = TrelloClient("k", "t", base_url=API, client=injected)
            card = await tc.create_card(list_id="l1", name="T")
        assert card["id"] == "c1"
