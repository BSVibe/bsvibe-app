"""ChatService — OpenAI-shape chat completions dispatcher.

Relocated out of the deleted ``backend.api.litellm_hook`` package (unified
routing Lift 2). ChatService now lives at ``backend.api.v1.chat_service``.
"""

from __future__ import annotations

import uuid

import pytest

from backend.api.v1.chat_service import ChatCompletionContext, ChatService


@pytest.mark.asyncio
async def test_chat_service_complete_rejects_missing_account() -> None:
    """Without account_id, complete() refuses — every dispatch needs scoping."""
    from unittest.mock import AsyncMock, MagicMock

    svc = ChatService(
        session=MagicMock(),
        budget=MagicMock(),
        accounts=MagicMock(),
        llm=MagicMock(chat=AsyncMock()),
        cipher=MagicMock(),
    )
    cctx = ChatCompletionContext(
        workspace_id=uuid.uuid4(),
        account_id=None,
        trace_id="t",
        stream=False,
        model_account_id=None,
    )
    with pytest.raises(ValueError, match="account_id"):
        await svc.complete(context=cctx, payload={"messages": []})
