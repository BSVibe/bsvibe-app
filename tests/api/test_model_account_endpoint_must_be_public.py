"""Saving a model account that points at a non-public endpoint is refused at the door.

The completion path refuses such an endpoint on every call (``backend.router.egress``,
``tests/router/test_llm_egress_is_public_only.py``) — but only once a run is already
driving, where the refusal costs retries and surfaces as a crashed drive. Saving is where the
founder can still fix it, so the same check answers there with a 422.

IP literals only: nothing here resolves a name.
"""

from __future__ import annotations

import pytest

from .test_first_account_becomes_default import client, db  # noqa: F401 — fixtures

pytestmark = pytest.mark.asyncio

_BODY = {"provider": "openai", "label": "x", "litellm_model": "openai/gpt-4o", "api_key": "k"}


@pytest.mark.parametrize(
    "extra",
    [
        {"api_base": "http://169.254.169.254/latest"},
        {"api_base": "http://10.0.0.5:11434"},
        {"extra_params": {"base_url": "http://127.0.0.1:6379"}},
        {"litellm_model": "ollama/llama3"},
    ],
)
async def test_creating_an_account_aimed_inward_is_a_422(client, extra) -> None:  # noqa: F811
    c, _ws, _db = client

    r = await c.post("/api/v1/accounts", json={**_BODY, **extra})

    assert r.status_code == 422, r.text
    assert "public" in r.text
    assert (await c.get("/api/v1/accounts")).json() == []


async def test_repointing_an_account_inward_is_a_422_and_changes_nothing(client) -> None:  # noqa: F811
    c, _ws, _db = client
    created = (await c.post("/api/v1/accounts", json=_BODY)).json()

    r = await c.patch(
        f"/api/v1/accounts/{created['id']}", json={"api_base": "http://192.168.0.2:8000"}
    )

    assert r.status_code == 422, r.text
    assert (await c.get(f"/api/v1/accounts/{created['id']}")).json()["api_base"] is None


async def test_a_public_endpoint_is_saved(client) -> None:  # noqa: F811
    """Control: the check refuses inward endpoints, not endpoints."""
    c, _ws, _db = client

    r = await c.post("/api/v1/accounts", json={**_BODY, "api_base": "https://93.184.216.34/v1"})

    assert r.status_code == 201, r.text


async def test_an_executor_account_is_not_checked(client) -> None:  # noqa: F811
    """An executor account dispatches to the founder's own WORKER, never from the server — so
    an opencode executor running the worker machine's own Ollama (``ollama/…``, no endpoint)
    is legitimate. The same model on a server-side account would call the server's loopback."""
    c, _ws, _db = client

    r = await c.post(
        "/api/v1/accounts",
        json={
            "provider": "executor",
            "label": "w",
            "litellm_model": "ollama/qwen3",
            "api_key": "k",
            "extra_params": {"executor_type": "opencode"},
        },
    )

    assert r.status_code == 201, r.text
