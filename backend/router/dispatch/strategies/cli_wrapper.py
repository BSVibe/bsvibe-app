"""CLI-wrapper dispatch strategy — the executor (claude_code / codex / opencode) branch.

After Lift D the ``provider == "executor"`` predicate has one home: the
:func:`~backend.router.dispatch.strategies.resolve_strategy_kind` resolver. The
*invoke* side of that branch — actually picking a worker, framing the prompt,
awaiting completion, and converging on the native verification contract —
lives in :class:`~backend.executors.coordinator.ExecutorOrchestrator`. This
strategy is the thin Router-facing wrapper around it: a caller asks the
resolver for a strategy and gets one entry point to construct the orchestrator
(today) or — when the Router facade is wired (Lift I) — to actually drive
``run`` through ``Router.invoke``.

The wrapping is intentionally minimal. The executor invoke side already
satisfies the :class:`~backend.execution.orchestrator.RunCompute` Protocol the
:class:`~backend.workflow.application.agent_runner.AgentRunner` calls today; rewiring
the AgentRunner to ``Router.invoke`` is a later lift, NOT this one (per the
v8 §13 Lift D scope-control invariant: build the strategy infrastructure, do
NOT collapse the call sites further than the predicate).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.executors.coordinator import ExecutorOrchestrator
from backend.router.accounts.models import ModelAccount
from backend.workflow.application.agent_loop import LoopResult
from backend.workflow.application.verification_service import CanonRetriever, JudgeLlm
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import SandboxManager


class CliWrapperStrategy:
    """Dispatch strategy for executor accounts (CLI worker pool).

    Thin wrapper around :class:`ExecutorOrchestrator` — constructs it from the
    same seams ``workers/run.py`` already threads (session + redis + sandbox +
    optional retriever + verify LLM) and forwards :meth:`execute` to the
    orchestrator's ``run``.

    The wrapper exists so that:

    1. The Router has ONE entry point to call into for an executor account
       (``CliWrapperStrategy(...).execute(run=..., workspace_dir=...)``); the
       ``provider == "executor"`` predicate stays at the strategy resolver.
    2. A future lift can swap the underlying orchestrator (e.g. a streaming
       implementation, a remote MCP-driven worker) without touching every
       call site that currently constructs ExecutorOrchestrator directly.

    The legacy call site (``workers/run.py``) still constructs the
    orchestrator inline today — that's part of the deliberate scope control
    in Lift D. A follow-up lift wires the strategy through the Router facade.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        redis: Any,
        account: ModelAccount,
        sandbox_manager: SandboxManager,
        settings: Settings | None = None,
        retriever: CanonRetriever | None = None,
        verify_llm: JudgeLlm | None = None,
    ) -> None:
        self._orchestrator = ExecutorOrchestrator(
            session=session,
            redis=redis,
            account=account,
            sandbox_manager=sandbox_manager,
            settings=settings,
            retriever=retriever,
            verify_llm=verify_llm,
        )

    async def execute(self, *, run: ExecutionRun, workspace_dir: Path) -> LoopResult:
        """Drive one executor run end-to-end (dispatch → verify → terminal).

        Same shape as the wrapped :meth:`ExecutorOrchestrator.run` so a future
        :meth:`Router.invoke` can call straight through."""
        return await self._orchestrator.run(run=run, workspace_dir=workspace_dir)


__all__ = ["CliWrapperStrategy"]
