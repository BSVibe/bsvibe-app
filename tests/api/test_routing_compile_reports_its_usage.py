"""#954 — compiling a routing rule from text reports its LLM usage.

The routing-rule compiler (``POST /run-routing-rules/compile`` and the create /
update paths that compile ``source_text``) runs one LLM turn on the
``routing.compile`` model, outside any run. #953 made every other run-less call
report under ``llm_usage_unattributed`` so the un-metered remainder can be
counted; this seam is not the dispatcher's ``CompileLlm``, so it was missed and
dropped the usage with no trace at all.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from structlog.testing import capture_logs

from backend.api.v1.run_routing import _AdapterCompileLlm
from backend.dispatch.adapter import ChatResponse
from backend.dispatch.caller_registry import CALLER_ROUTING_COMPILE
from backend.workflow.application.runtime.dispatcher import UNATTRIBUTED_USAGE_EVENT

pytestmark = pytest.mark.asyncio


class _Adapter:
    def __init__(self, response: ChatResponse) -> None:
        self._response = response

    async def chat(self, **_: Any) -> ChatResponse:
        return self._response


async def test_a_compile_turn_reports_its_tokens_as_unattributed() -> None:
    ws = uuid.uuid4()
    llm = _AdapterCompileLlm(
        _Adapter(ChatResponse(content="[]", usage_prompt_tokens=420, usage_completion_tokens=37)),
        workspace_id=ws,
    )
    with capture_logs() as logs:
        assert await llm.complete_text(system="s", user="u") == "[]"

    events = [e for e in logs if e["event"] == UNATTRIBUTED_USAGE_EVENT]
    assert len(events) == 1
    assert events[0]["site"] == CALLER_ROUTING_COMPILE
    assert events[0]["workspace_id"] == str(ws)
    assert events[0]["usage_prompt_tokens"] == 420
    assert events[0]["usage_completion_tokens"] == 37
