"""OpenFGAClient tests — httpx mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.fixture
def fga_settings():
    from backend.shared.authz.settings import Settings

    return Settings(  # type: ignore[call-arg]
        bsvibe_auth_url="https://auth.bsvibe.dev",
        openfga_api_url="http://openfga.local:8080",
        openfga_store_id="store-1",
        openfga_auth_model_id="model-1",
        openfga_auth_token="fga-secret",
        service_token_signing_secret="x",
    )


async def test_check_returns_true_when_allowed(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock(assert_all_called=True) as r:
        route = r.post(
            "http://openfga.local:8080/stores/store-1/check",
        ).mock(return_value=httpx.Response(200, json={"allowed": True}))

        async with OpenFGAClient(fga_settings) as client:
            allowed = await client.check(
                user="user:alice",
                relation="viewer",
                object_="project:p1",
            )

        assert allowed is True
        body = route.calls.last.request.content
        assert b'"user":"user:alice"' in body
        assert b'"relation":"viewer"' in body
        assert b'"object":"project:p1"' in body


async def test_check_returns_false_when_denied(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock() as r:
        r.post("http://openfga.local:8080/stores/store-1/check").mock(
            return_value=httpx.Response(200, json={"allowed": False}),
        )
        async with OpenFGAClient(fga_settings) as client:
            assert await client.check("user:alice", "editor", "project:p1") is False


async def test_check_attaches_bearer_when_auth_token_set(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock() as r:
        route = r.post("http://openfga.local:8080/stores/store-1/check").mock(
            return_value=httpx.Response(200, json={"allowed": True}),
        )
        async with OpenFGAClient(fga_settings) as client:
            await client.check("user:a", "v", "p:1")

        auth = route.calls.last.request.headers.get("authorization")
        assert auth == "Bearer fga-secret"


async def test_check_includes_auth_model_id(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock() as r:
        route = r.post("http://openfga.local:8080/stores/store-1/check").mock(
            return_value=httpx.Response(200, json={"allowed": True}),
        )
        async with OpenFGAClient(fga_settings) as client:
            await client.check("user:a", "v", "p:1")

        body = route.calls.last.request.content
        assert b'"authorization_model_id":"model-1"' in body


async def test_check_raises_on_5xx(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient, OpenFGAError

    with respx.mock() as r:
        r.post("http://openfga.local:8080/stores/store-1/check").mock(
            return_value=httpx.Response(500, json={"error": "boom"}),
        )
        async with OpenFGAClient(fga_settings) as client:
            with pytest.raises(OpenFGAError):
                await client.check("user:a", "v", "p:1")


async def test_check_raises_on_401(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAAuthError, OpenFGAClient

    with respx.mock() as r:
        r.post("http://openfga.local:8080/stores/store-1/check").mock(
            return_value=httpx.Response(401, json={"error": "unauthenticated"}),
        )
        async with OpenFGAClient(fga_settings) as client:
            with pytest.raises(OpenFGAAuthError):
                await client.check("user:a", "v", "p:1")


async def test_list_objects_returns_object_ids(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock() as r:
        r.post("http://openfga.local:8080/stores/store-1/list-objects").mock(
            return_value=httpx.Response(
                200,
                json={"objects": ["project:p1", "project:p2"]},
            ),
        )
        async with OpenFGAClient(fga_settings) as client:
            objects = await client.list_objects(
                user="user:alice",
                relation="viewer",
                type_="project",
            )
        assert objects == ["project:p1", "project:p2"]


async def test_write_tuple(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock() as r:
        route = r.post("http://openfga.local:8080/stores/store-1/write").mock(
            return_value=httpx.Response(200, json={}),
        )
        async with OpenFGAClient(fga_settings) as client:
            await client.write_tuple(
                user="user:alice",
                relation="owner",
                object_="project:p1",
            )
        body = route.calls.last.request.content
        assert b'"writes"' in body
        assert b'"user":"user:alice"' in body


async def test_delete_tuple(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    with respx.mock() as r:
        route = r.post("http://openfga.local:8080/stores/store-1/write").mock(
            return_value=httpx.Response(200, json={}),
        )
        async with OpenFGAClient(fga_settings) as client:
            await client.delete_tuple(
                user="user:alice",
                relation="owner",
                object_="project:p1",
            )
        body = route.calls.last.request.content
        assert b'"deletes"' in body


async def test_check_uses_3s_timeout(fga_settings) -> None:
    from backend.shared.authz.client import OpenFGAClient

    async with OpenFGAClient(fga_settings) as client:
        # Default timeout in settings is 3s — surface it on the underlying httpx client.
        assert client._http.timeout.read == 3.0  # noqa: SLF001
