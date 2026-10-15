"""A tenant's model account cannot point the server at its own network.

``ModelAccount.api_base`` is whatever the tenant typed, and ``extra_params`` is a free dict
merged straight into ``litellm.acompletion``'s kwargs. Nothing checked either: a workspace
could set ``api_base`` to ``http://postgres:5432``, ``http://169.254.169.254`` or the operator's
own Ollama, and the backend container would make that request on every turn and hand the
reply (or the error body) back to the tenant. With public signup that is server-side request
forgery against the operator's box.

The guard sits in :meth:`LlmClient.chat` because it is the one place every tenant-configured
completion passes (the LiteLLM adapter and the chat-completions proxy both call it), and it
checks the FINAL kwargs — after ``extra_params`` is merged — so ``extra_params.base_url`` is
not a way around it. A hostname is resolved and every address must be global: a public name
that resolves to a private address is the same request.

DNS is injected (``resolver=``): no test here touches the network.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.router.llm_client import EgressRefused, LlmClient

pytestmark = pytest.mark.asyncio

_PUBLIC = "93.184.216.34"


def _resolver(table: dict[str, list[str]]) -> Any:
    async def _resolve(host: str) -> list[str]:
        if host not in table:
            raise socket.gaierror(f"unknown host {host}")
        return table[host]

    return _resolve


def _ok() -> AsyncMock:
    return AsyncMock(
        return_value={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )


async def _chat(client: LlmClient, **kw: Any) -> Any:
    return await client.chat(messages=[{"role": "user", "content": "hi"}], **kw)


@pytest.mark.parametrize(
    "api_base",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://10.0.0.5/v1",
        "http://172.18.0.3:5432",
        "http://192.168.1.10:8000",
        "http://169.254.169.254/latest/meta-data",
        "http://100.101.102.103:11434",  # tailnet / CGNAT
        "http://[::1]:8000",
        "http://0.0.0.0:6379",
    ],
)
async def test_a_non_public_api_base_is_refused_before_any_request(api_base: str) -> None:
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({"localhost": ["127.0.0.1"]}))

    with pytest.raises(EgressRefused):
        await _chat(client, model="openai/gpt-4o", api_base=api_base, api_key="k")

    completion.assert_not_awaited()


async def test_a_public_name_that_resolves_inward_is_refused() -> None:
    """The name is not what is reached — its addresses are."""
    completion = _ok()
    client = LlmClient(
        completion_fn=completion,
        resolver=_resolver({"llm.attacker.example": [_PUBLIC, "10.1.2.3"]}),
    )

    with pytest.raises(EgressRefused):
        await _chat(client, model="openai/x", api_base="https://llm.attacker.example/v1")

    completion.assert_not_awaited()


async def test_a_docker_service_name_is_refused() -> None:
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({"postgres": ["172.18.0.2"]}))

    with pytest.raises(EgressRefused):
        await _chat(client, model="openai/x", api_base="http://postgres:5432")


async def test_extra_params_cannot_carry_the_endpoint_around_the_check() -> None:
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({}))

    with pytest.raises(EgressRefused):
        await _chat(
            client,
            model="openai/x",
            api_key="k",
            extra_params={"base_url": "http://169.254.169.254"},
        )

    completion.assert_not_awaited()


async def test_extra_params_cannot_replace_a_checked_api_base() -> None:
    """``extra_params`` is merged AFTER ``api_base`` — the check must read the merged kwargs."""
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({"api.example.com": [_PUBLIC]}))

    with pytest.raises(EgressRefused):
        await _chat(
            client,
            model="openai/x",
            api_base="https://api.example.com",
            extra_params={"api_base": "http://10.0.0.9"},
        )


@pytest.mark.parametrize("model", ["ollama/llama3", "ollama_chat/qwen", "hosted_vllm/m"])
async def test_a_local_runtime_model_without_an_endpoint_is_refused(model: str) -> None:
    """With no ``api_base`` LiteLLM sends these to localhost — the server's own loopback."""
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({}))

    with pytest.raises(EgressRefused):
        await _chat(client, model=model)

    completion.assert_not_awaited()


async def test_a_non_http_scheme_is_refused() -> None:
    client = LlmClient(completion_fn=_ok(), resolver=_resolver({}))

    with pytest.raises(EgressRefused):
        await _chat(client, model="openai/x", api_base="file:///etc/passwd")


async def test_an_unresolvable_host_is_refused() -> None:
    client = LlmClient(completion_fn=_ok(), resolver=_resolver({}))

    with pytest.raises(EgressRefused):
        await _chat(client, model="openai/x", api_base="https://nowhere.invalid")


# ── Controls ─────────────────────────────────────────────────────────────────


async def test_a_public_api_base_goes_through() -> None:
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({"api.example.com": [_PUBLIC]}))

    response = await _chat(client, model="openai/x", api_base="https://api.example.com/v1")

    assert response.content == "ok"
    assert completion.await_args.kwargs["api_base"] == "https://api.example.com/v1"


async def test_a_hosted_provider_with_no_endpoint_goes_through() -> None:
    """The ordinary BYO-key account: the provider's own public endpoint, nothing to check."""
    completion = _ok()
    client = LlmClient(completion_fn=completion, resolver=_resolver({}))

    await _chat(client, model="anthropic/claude-sonnet-5-5", api_key="k")

    completion.assert_awaited_once()


async def test_an_operator_allowlisted_host_goes_through() -> None:
    """The operator's escape hatch (``llm_egress_allowed_hosts``) — e.g. their own Ollama for
    their own workspace. Exact host match; nothing else is opened by it."""
    completion = _ok()
    client = LlmClient(
        completion_fn=completion,
        resolver=_resolver({"host.docker.internal": ["192.168.5.2"]}),
        allowed_hosts=frozenset({"host.docker.internal"}),
    )

    await _chat(client, model="ollama/bge-m3", api_base="http://host.docker.internal:11434")

    completion.assert_awaited_once()
