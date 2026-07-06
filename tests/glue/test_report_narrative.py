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


@pytest.mark.asyncio
async def test_narrate_writes_in_the_workspace_language(monkeypatch) -> None:
    """A ``ko`` workspace makes the narrative generate in Korean — the language
    directive is appended to the system prompt so the model writes user-facing
    prose in the founder's language (code / identifiers stay verbatim)."""
    chat = _StubChat("퍼센트 변화를 계산하는 도우미를 추가했어요.")
    svc = ReportNarrativeService.__new__(ReportNarrativeService)
    monkeypatch.setattr(svc, "_resolve_chat", lambda workspace_id: _ready(chat))

    out = await svc.narrate(
        workspace_id=uuid.uuid4(),
        intent="퍼센트 변화 유틸 추가",
        summary="backend/common/percent_change.py",
        diff="+def percent_change(old, new): ...",
        language="ko",
    )
    assert out == "퍼센트 변화를 계산하는 도우미를 추가했어요."
    system = next(m["content"] for m in chat.seen[0] if m["role"] == "system")
    assert "Korean" in system  # the language directive was appended


@pytest.mark.asyncio
async def test_narrate_english_workspace_has_no_language_directive(monkeypatch) -> None:
    """The default (``en``) adds nothing — an English workspace pays zero prompt
    overhead (the directive is empty)."""
    chat = _StubChat("Added a helper.")
    svc = ReportNarrativeService.__new__(ReportNarrativeService)
    monkeypatch.setattr(svc, "_resolve_chat", lambda workspace_id: _ready(chat))

    await svc.narrate(workspace_id=uuid.uuid4(), intent="x", summary="y", diff=None, language="en")
    system = next(m["content"] for m in chat.seen[0] if m["role"] == "system")
    assert "Korean" not in system
    assert "Write all user-facing prose" not in system


async def _ready(value):
    return value
