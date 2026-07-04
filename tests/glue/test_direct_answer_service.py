"""DirectAnswerService — inline answer treats executor + LiteLLM identically.

Founder design intent: ``ExecutorAdapter`` and ``LiteLLMAdapter`` are abstracted
to the SAME ``chat()`` interface (``is_executor_account`` is the only branch;
the executor differs only in subscription-cost billing, not function). So an
inline Direct answer (``POST /api/v1/messages/ask``) dispatches to whichever
account the workspace routed — executor INCLUDED — instead of special-casing
executors away.

Two invariants preserved from the original J2 fix:
* the endpoint NEVER 500s — any failure (executor at capacity / timeout / LLM
  error) degrades to ``None`` (answered=false → the PWA dispatches async work);
* the synchronous HTTP wait on an executor task is BOUNDED by a short inline
  timeout so a slow executor degrades to async rather than blocking the request
  for the full frame timeout.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import backend.workflow.application.direct_answer as da
from backend.dispatch.adapter import ExecutorAdapterUnavailable
from backend.workflow.application.direct_answer import DirectAnswerService
from backend.workflow.infrastructure.db import RunStatus

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def scalars(self) -> list[Any]:
        return self._items


class _FakeSession:
    """Minimal AsyncSession stand-in: ``get`` returns a product, ``execute``
    returns queued results in call order (runs query, then deliverables)."""

    def __init__(self, product: Any, results: list[list[Any]]) -> None:
        self._product = product
        self._results = [_Result(r) for r in results]

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self._product

    async def execute(self, _stmt: Any) -> Any:
        return self._results.pop(0)


def _capturing_adapter(captured: dict[str, Any]) -> Any:
    class _Adapter:
        timeout_s: float | None = None

        async def chat(self, *, system: str, messages: list[dict[str, Any]], tools: Any) -> Any:
            captured["system"] = system
            captured["messages"] = messages
            return SimpleNamespace(content="Here is the status.", tool_calls=(), artifact_refs=())

    return _Adapter()


async def _empty_retrieve(self: Any, *_a: Any, **_k: Any) -> list[str]:
    return []


async def test_product_context_is_injected_when_product_id_given(tmp_path, monkeypatch) -> None:
    """When ``product_id`` names a product in the workspace, its name, repo, and
    recent runs (with status) are injected as grounding — so "how's the project?"
    is answered from real state, not an empty sandbox."""
    ws, pid, rid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    product = SimpleNamespace(workspace_id=ws, name="toolkit", repo_url="blas1n/toolkit")
    run = SimpleNamespace(
        id=rid, status=RunStatus.REVIEW_READY, payload={"intent_text": "raw intent"}
    )
    deliverable = SimpleNamespace(run_id=rid, payload={"summary": "Add a title-case helper"})
    session = _FakeSession(product, [[run], [deliverable]])

    captured: dict[str, Any] = {}
    adapter = _capturing_adapter(captured)
    monkeypatch.setattr(
        da,
        "_resolve_via_caller",
        lambda *_a, **_k: _async(
            SimpleNamespace(account=SimpleNamespace(provider="litellm"), adapter=adapter)
        ),
    )
    monkeypatch.setattr(DirectAnswerService, "_retrieve", _empty_retrieve)

    settings = SimpleNamespace(
        knowledge_default_region="us-1", knowledge_vault_root=str(tmp_path / "vault")
    )
    svc = DirectAnswerService(session=session, settings=settings)  # type: ignore[arg-type]
    out = await svc.answer(workspace_id=ws, product_id=pid, text="현재 프로젝트 상황 어때?")

    assert out == "Here is the status."
    grounding = (
        captured["system"]
        + "\n"
        + "\n".join(
            str(m.get("content")) for m in captured["messages"] if m.get("role") == "system"
        )
    )
    assert "toolkit" in grounding
    assert "blas1n/toolkit" in grounding
    # deliverable summary is preferred over the raw run intent for the title
    assert "Add a title-case helper" in grounding
    assert "ready to ship" in grounding
    # and it explicitly tells the model NOT to inspect an empty working directory
    assert "do NOT inspect" in grounding.lower() or "do not inspect" in grounding.lower()


async def test_product_in_another_workspace_is_not_injected(tmp_path, monkeypatch) -> None:
    """Defense-in-depth: a product_id whose product belongs to a DIFFERENT
    workspace injects no grounding (and never leaks its state)."""
    ws, other_ws, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    product = SimpleNamespace(workspace_id=other_ws, name="secret-product", repo_url=None)
    session = _FakeSession(product, [])  # execute must never be reached

    captured: dict[str, Any] = {}
    adapter = _capturing_adapter(captured)
    monkeypatch.setattr(
        da,
        "_resolve_via_caller",
        lambda *_a, **_k: _async(
            SimpleNamespace(account=SimpleNamespace(provider="litellm"), adapter=adapter)
        ),
    )
    monkeypatch.setattr(DirectAnswerService, "_retrieve", _empty_retrieve)

    settings = SimpleNamespace(
        knowledge_default_region="us-1", knowledge_vault_root=str(tmp_path / "vault")
    )
    svc = DirectAnswerService(session=session, settings=settings)  # type: ignore[arg-type]
    out = await svc.answer(workspace_id=ws, product_id=pid, text="status?")

    assert out == "Here is the status."
    grounding = (
        captured["system"]
        + "\n"
        + "\n".join(
            str(m.get("content")) for m in captured["messages"] if m.get("role") == "system"
        )
    )
    assert "secret-product" not in grounding


def _async(value: Any) -> Any:
    async def _coro() -> Any:
        return value

    return _coro()


def _service(tmp_path, *, redis: Any = None) -> DirectAnswerService:
    settings = SimpleNamespace(
        knowledge_default_region="us-1",
        knowledge_vault_root=str(tmp_path / "vault"),
    )
    return DirectAnswerService(session=None, settings=settings, redis=redis)  # type: ignore[arg-type]


async def test_executor_chat_account_answers_inline(tmp_path, monkeypatch) -> None:
    """An executor account now ANSWERS inline (functional parity) — it is no
    longer special-cased away. The redis transport is threaded to the resolver
    and the adapter's wait is bounded to the inline timeout."""
    seen: dict[str, Any] = {}

    class _ExecAdapter:
        timeout_s: float | None = 300.0

        async def chat(self, **_kw: Any) -> Any:
            seen["timeout_s"] = self.timeout_s
            return SimpleNamespace(content="X is a thing.", tool_calls=(), artifact_refs=())

    resolved = SimpleNamespace(account=SimpleNamespace(provider="executor"), adapter=_ExecAdapter())
    captured: dict[str, Any] = {}

    async def _fake_resolve(*_a: Any, **kw: Any) -> Any:
        captured.update(kw)
        return resolved

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path, redis="REDIS").answer(
        workspace_id=uuid.uuid4(), text="What is X?"
    )
    assert out == "X is a thing."
    # redis threaded so the executor adapter has a worker-stream transport
    assert captured.get("redis") == "REDIS"
    # the synchronous HTTP wait is bounded to the inline timeout (not 300s frame)
    assert seen["timeout_s"] == da._INLINE_ANSWER_TIMEOUT_S


async def test_executor_failure_degrades_to_none(tmp_path, monkeypatch) -> None:
    """An executor that fails / is at capacity / times out degrades to None
    (answered=false → async dispatch) — the endpoint still never 500s."""

    class _FailingExecutor:
        timeout_s: float | None = 300.0

        async def chat(self, **_kw: Any) -> Any:
            raise ExecutorAdapterUnavailable("worker at capacity")

    resolved = SimpleNamespace(
        account=SimpleNamespace(provider="executor"), adapter=_FailingExecutor()
    )

    async def _fake_resolve(*_a: Any, **_k: Any) -> Any:
        return resolved

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path).answer(workspace_id=uuid.uuid4(), text="What is X?")
    assert out is None


async def test_llm_failure_degrades_to_none_not_500(tmp_path, monkeypatch) -> None:
    """Any LLM failure on the inline path is swallowed → None, so the endpoint
    never 500s (the 500 was what the browser saw as a CORS error)."""

    class _FailingAdapter:
        timeout_s: float | None = None

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
    """A native (LiteLLM) chat account answers inline — unchanged."""

    class _OkAdapter:
        timeout_s: float | None = None

        async def chat(self, **_kw: Any) -> Any:
            return SimpleNamespace(content="X is a thing.", tool_calls=(), artifact_refs=())

    resolved = SimpleNamespace(account=SimpleNamespace(provider="litellm"), adapter=_OkAdapter())

    async def _fake_resolve(*_a: Any, **_k: Any) -> Any:
        return resolved

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path).answer(workspace_id=uuid.uuid4(), text="What is X?")
    assert out == "X is a thing."


async def test_no_chat_account_returns_none(tmp_path, monkeypatch) -> None:
    """No account resolves → None (caller dispatches as work)."""

    async def _fake_resolve(*_a: Any, **_k: Any) -> Any:
        return None

    monkeypatch.setattr(da, "_resolve_via_caller", _fake_resolve)

    out = await _service(tmp_path).answer(workspace_id=uuid.uuid4(), text="What is X?")
    assert out is None
