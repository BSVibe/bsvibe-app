"""R1 — the report leads with a plain-language "what this did" narrative.

The founder's redesigned report needs a human description of WHAT the work did
(not the raw changed-file list). A chat model composes a 2-3 sentence summary
from the intent + the captured diff; it is generated lazily on first report view
and cached on the deliverable so later views are instant. No chat model → None
(the report falls back to the request line), never a crash.
"""

from __future__ import annotations

import uuid

import pytest

from backend.workflow.application.report_narrative import ReportNarrativeService


class _StubChat:
    """A ResolverLoopLlm-shaped stub: returns a canned completion."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.seen: list[list[dict]] = []

    async def complete(self, *, messages, tools):  # type: ignore[no-untyped-def]
        self.seen.append(list(messages))
        from backend.workflow.application.agent_loop import LoopTurn

        return LoopTurn(content=self._content, tool_calls=())


@pytest.mark.asyncio
async def test_narrate_composes_from_intent_and_diff(monkeypatch) -> None:
    chat = _StubChat("Added a percent_change() helper that rounds to 2 decimals.")
    svc = ReportNarrativeService.__new__(ReportNarrativeService)
    # Inject the chat directly (bypass account resolution for the unit test).
    monkeypatch.setattr(svc, "_resolve_chat", lambda workspace_id: _ready(chat))

    out = await svc.narrate(
        workspace_id=uuid.uuid4(),
        intent="Add a percent_change utility",
        summary="backend/common/percent_change.py, tests/test_percent_change.py",
        diff="+def percent_change(old, new):\n+    if old == 0: raise ValueError",
    )
    assert "percent_change" in out
    # The diff + intent were folded into the prompt.
    joined = " ".join(m["content"] for batch in chat.seen for m in batch)
    assert "percent_change" in joined


@pytest.mark.asyncio
async def test_narrate_returns_none_without_chat(monkeypatch) -> None:
    svc = ReportNarrativeService.__new__(ReportNarrativeService)
    monkeypatch.setattr(svc, "_resolve_chat", lambda workspace_id: _ready(None))
    out = await svc.narrate(workspace_id=uuid.uuid4(), intent="x", summary="y", diff=None)
    assert out is None


async def _ready(value):
    return value
