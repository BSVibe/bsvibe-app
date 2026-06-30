"""DirectAnswerService never 500s — graceful inline-answer degrade (J2 fix).

Live e2e found the Direct question path (`POST /api/v1/messages/ask`) returning
500 → the browser surfaced it as a CORS error ("Network hiccup"). Root cause:
the chat model resolved to an EXECUTOR account (CALLER_FRAME fell back to one),
and ``llm.complete`` raised ``ExecutorAdapterUnavailable`` unhandled. An inline
synchronous answer must NEVER crash — it degrades to ``None`` (answered=false),
and the PWA dispatches the text as async work instead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import backend.workflow.application.direct_answer as da
from backend.workflow.application.direct_answer import DirectAnswerService

pytestmark = pytest.mark.asyncio


def _service(tmp_path) -> DirectAnswerService:
    settings = SimpleNamespace(
        knowledge_default_region="us-1",
        knowledge_vault_root=str(tmp_path / "vault"),
    )
    return DirectAnswerService(session=None, settings=settings)  # type: ignore[arg-type]


async def test_executor_chat_account_degrades_to_none(tmp_path, monkeypatch) -> None:
    """An inline answer can't dispatch to an EXECUTOR account — it degrades to
    None (→ answered=false → async dispatch), never attempting + crashing."""

    class _Boom:
        async def chat(self, **_kw: Any) -> Any:
            raise AssertionError("executor adapter must NOT be invoked for inline chat")

    resolved = SimpleNamespace(account=SimpleNamespace(provider="executor"), adapter=_Boom())

    async def _fake_resolve(*_a: Any, **_k: Any) -> Any:
        return resolved

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path).answer(workspace_id=uuid.uuid4(), text="What is X?")
    assert out is None


async def test_llm_failure_degrades_to_none_not_500(tmp_path, monkeypatch) -> None:
    """Any LLM failure on the inline path is swallowed → None, so the endpoint
    never 500s (the 500 was what the browser saw as a CORS error)."""

    class _FailingAdapter:
        async def chat(self, **_kw: Any) -> Any:
            raise RuntimeError("model timed out")

    resolved = SimpleNamespace(
        account=SimpleNamespace(provider="litellm"), adapter=_FailingAdapter()
    )

    async def _fake_resolve(*_a: Any, **_k: Any) -> Any:
        return resolved

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path).answer(workspace_id=uuid.uuid4(), text="What is X?")
    assert out is None


async def test_native_chat_account_answers(tmp_path, monkeypatch) -> None:
    """A real (non-executor) chat account still answers inline — the degrade
    only fires for executors / failures, not for a working chat model."""

    class _OkAdapter:
        async def chat(self, **_kw: Any) -> Any:
            return SimpleNamespace(content="X is a thing.", tool_calls=(), artifact_refs=())

    resolved = SimpleNamespace(account=SimpleNamespace(provider="litellm"), adapter=_OkAdapter())

    async def _fake_resolve(*_a: Any, **_k: Any) -> Any:
        return resolved

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path).answer(workspace_id=uuid.uuid4(), text="What is X?")
    assert out == "X is a thing."
