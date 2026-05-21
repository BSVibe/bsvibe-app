"""LiteLLM hook + ChatService skeleton smoke."""

from __future__ import annotations

import uuid

import pytest

from backend.api.litellm_hook import ChatService, LiteLLMHook
from backend.api.litellm_hook.chat_service import ChatCompletionContext
from backend.api.litellm_hook.hook import HookContext


def _ctx() -> HookContext:
    return HookContext(
        workspace_id=uuid.uuid4(),
        account_id=None,
        user_id=None,
        trace_id="trace-1",
    )


@pytest.mark.asyncio
async def test_hook_passthrough_returns_data() -> None:
    hook = LiteLLMHook(context=_ctx())
    payload = {"model": "x", "messages": []}
    out = await hook.async_pre_call_hook(payload)
    assert out is payload


@pytest.mark.asyncio
async def test_chat_service_complete_not_implemented() -> None:
    svc = ChatService()
    cctx = ChatCompletionContext(
        workspace_id=uuid.uuid4(), account_id=None, trace_id="t", stream=False
    )
    with pytest.raises(NotImplementedError, match="Bundle API skeleton"):
        await svc.complete(context=cctx, payload={})


@pytest.mark.asyncio
async def test_chat_service_stream_not_implemented() -> None:
    svc = ChatService()
    cctx = ChatCompletionContext(
        workspace_id=uuid.uuid4(), account_id=None, trace_id="t", stream=True
    )
    with pytest.raises(NotImplementedError):
        async for _chunk in svc.stream(context=cctx, payload={}):
            pass
