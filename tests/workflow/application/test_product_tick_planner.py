"""ProductTickPlanner — the dedicated product-tick planner (RED→GREEN).

The planner STRUCTURALLY reads product state + accumulated knowledge + recent
run history and composes ONE in-process cheap-LLM call that returns a CONCRETE
next-action instruction (the run's glass-box intent). Any failure — no route,
missing product, workspace mismatch, unparseable LLM output — degrades to
``None`` so the tick still runs on the static meta-instruction fallback.

The LLM is ALWAYS mocked (never a real API): ``_resolve_via_caller`` is patched
to hand back a fake adapter whose ``chat`` records the composed messages and
returns scripted content. ``build_canon_retriever`` is patched to a fake
retriever. Product + run rows are real (in-memory SQLite).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import backend.workflow.application.product_tick_planner as planner_mod
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.workflow.application.product_tick_planner import ProductTickPlanner, TickPlan
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from tests._support import memory_session

pytestmark = pytest.mark.asyncio


class _FakeAdapter:
    """Records the ``chat`` payload and returns scripted content (no real API)."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> SimpleNamespace:
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        return SimpleNamespace(content=self._content, tool_calls=())


class _FakeRetriever:
    def __init__(self, snippets: list[str]) -> None:
        self._snippets = snippets
        self.seen_signals: list[str] = []

    async def retrieve_for_signals(self, signals: str) -> list[str]:
        self.seen_signals.append(signals)
        return list(self._snippets)


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, adapter: Any) -> None:
    async def _fake_resolve(*_args: Any, **_kwargs: Any) -> Any:
        if adapter is None:
            return None
        return SimpleNamespace(adapter=adapter)

    monkeypatch.setattr(planner_mod, "_resolve_via_caller", _fake_resolve)


def _patch_retriever(monkeypatch: pytest.MonkeyPatch, retriever: Any) -> None:
    monkeypatch.setattr(planner_mod, "build_canon_retriever", lambda *a, **k: retriever)


async def _seed_product(
    session: Any,
    *,
    workspace_id: uuid.UUID,
    product_id: uuid.UUID,
    language: str = "ko",
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(WorkspaceRow(id=workspace_id, name="Acme", language=language))
    session.add(
        ProductRow(
            id=product_id,
            workspace_id=workspace_id,
            name="Checkout Service",
            slug="checkout-service",
            repo_url="https://github.com/acme/checkout",
            product_metadata=metadata
            or {"goal": "Ship the MVP checkout flow", "lifecycle": "growth"},
        )
    )
    await session.flush()


async def _seed_runs(session: Any, *, workspace_id: uuid.UUID, product_id: uuid.UUID) -> None:
    now = datetime.now(tz=UTC)
    for i, intent in enumerate(["Add Stripe webhook handler", "Fix refund rounding bug"]):
        session.add(
            ExecutionRun(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                product_id=product_id,
                status=RunStatus.SHIPPED,
                payload={"intent_text": intent},
                created_at=now - timedelta(minutes=10 - i),
                updated_at=now,
            )
        )
    await session.flush()


async def test_plan_happy_path_returns_tickplan_and_composes_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    adapter = _FakeAdapter(
        '{"instruction": "Wire the Stripe webhook signature check", '
        '"rationale": "Last run added the handler but left verification open"}'
    )
    retriever = _FakeRetriever(["Insight: webhooks must verify signatures"])
    _patch_resolver(monkeypatch, adapter)
    _patch_retriever(monkeypatch, retriever)

    async with memory_session() as session:
        await _seed_product(session, workspace_id=workspace_id, product_id=product_id)
        await _seed_runs(session, workspace_id=workspace_id, product_id=product_id)

        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        plan = await planner.plan(workspace_id=workspace_id, product_id=product_id)

    assert isinstance(plan, TickPlan)
    assert plan.instruction == "Wire the Stripe webhook signature check"
    assert plan.rationale.startswith("Last run added the handler")

    # The single composed LLM call must structurally carry product metadata,
    # the run-history summary, and the knowledge snippets.
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    user_content = "\n".join(
        str(m.get("content", "")) for m in call["messages"] if m.get("role") == "user"
    )
    assert "Checkout Service" in user_content
    assert "Ship the MVP checkout flow" in user_content  # metadata goal
    assert "Add Stripe webhook handler" in user_content  # run history summary
    assert "Fix refund rounding bug" in user_content
    assert "webhooks must verify signatures" in user_content  # knowledge snippet
    # Localized: workspace language ko → the directive rides the system prompt.
    assert "Korean" in call["system"]
    # Signals sent to the retriever include the product name + goal-ish text.
    assert retriever.seen_signals
    assert "Checkout Service" in retriever.seen_signals[0]


async def test_plan_returns_none_when_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    _patch_resolver(monkeypatch, None)  # _resolve_via_caller → None
    _patch_retriever(monkeypatch, _FakeRetriever([]))

    async with memory_session() as session:
        await _seed_product(session, workspace_id=workspace_id, product_id=product_id)
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        plan = await planner.plan(workspace_id=workspace_id, product_id=product_id)

    assert plan is None


async def test_plan_returns_none_when_product_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(monkeypatch, _FakeAdapter("{}"))
    _patch_retriever(monkeypatch, _FakeRetriever([]))
    async with memory_session() as session:
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        plan = await planner.plan(workspace_id=uuid.uuid4(), product_id=uuid.uuid4())
    assert plan is None


async def test_plan_returns_none_on_workspace_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_ws = uuid.uuid4()
    product_id = uuid.uuid4()
    _patch_resolver(monkeypatch, _FakeAdapter("{}"))
    _patch_retriever(monkeypatch, _FakeRetriever([]))
    async with memory_session() as session:
        await _seed_product(session, workspace_id=real_ws, product_id=product_id)
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        # Ask under a DIFFERENT workspace → tenancy guard returns None.
        plan = await planner.plan(workspace_id=uuid.uuid4(), product_id=product_id)
    assert plan is None


async def test_plan_returns_none_on_unparseable_llm_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    _patch_resolver(monkeypatch, _FakeAdapter("not json at all, sorry"))
    _patch_retriever(monkeypatch, _FakeRetriever([]))
    async with memory_session() as session:
        await _seed_product(session, workspace_id=workspace_id, product_id=product_id)
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        plan = await planner.plan(workspace_id=workspace_id, product_id=product_id)
    assert plan is None


async def test_plan_returns_none_on_empty_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()
    _patch_resolver(monkeypatch, _FakeAdapter('{"instruction": "   ", "rationale": "meh"}'))
    _patch_retriever(monkeypatch, _FakeRetriever([]))
    async with memory_session() as session:
        await _seed_product(session, workspace_id=workspace_id, product_id=product_id)
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        plan = await planner.plan(workspace_id=workspace_id, product_id=product_id)
    assert plan is None


async def test_plan_never_raises_on_llm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()

    class _BoomAdapter:
        async def chat(self, **_kwargs: Any) -> Any:
            raise RuntimeError("gateway exploded")

    _patch_resolver(monkeypatch, _BoomAdapter())
    _patch_retriever(monkeypatch, _FakeRetriever([]))
    async with memory_session() as session:
        await _seed_product(session, workspace_id=workspace_id, product_id=product_id)
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        plan = await planner.plan(workspace_id=workspace_id, product_id=product_id)
    assert plan is None  # swallowed → static fallback


async def test_plan_reraises_capacity_saturated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A saturated executor (yield_on_saturation run-drive caller) surfaces
    :class:`ExecutorCapacitySaturated` out of the LLM call. The planner must
    RE-RAISE it — NOT swallow it to ``None`` — so the tick yields back instead
    of falling through to a static-instruction framing attempt that would also
    saturate the shared worker."""
    from backend.dispatch.adapter import ExecutorCapacitySaturated

    workspace_id = uuid.uuid4()
    product_id = uuid.uuid4()

    class _SaturatedAdapter:
        async def chat(self, **_kwargs: Any) -> Any:
            raise ExecutorCapacitySaturated("all live workers at capacity")

    _patch_resolver(monkeypatch, _SaturatedAdapter())
    _patch_retriever(monkeypatch, _FakeRetriever([]))
    async with memory_session() as session:
        await _seed_product(session, workspace_id=workspace_id, product_id=product_id)
        planner = ProductTickPlanner(session, settings=SimpleNamespace())
        with pytest.raises(ExecutorCapacitySaturated):
            await planner.plan(workspace_id=workspace_id, product_id=product_id)
