"""Workspace ModelAccount resolution policy (§17.2a slice).

Phase 2 v1 policy — "exactly one active non-executor account → use it; zero or
ambiguous → never guess, never stall: write a :class:`Decision` and return
``None``." Honors the founder-in-the-loop invariant: stuck → Decision, never a
silent stall.

Extracted out of the legacy ``backend.workflow.infrastructure.workers.run``
god-file. Three callsites share these helpers (the run resolver, the judge
resolver, the frame-LLM resolver), so they sit together here.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings
from backend.router.accounts.models import ModelAccount
from backend.router.dispatch.strategies import is_executor_account
from backend.workflow.application.loop_llm import GatewayLoopLlm
from backend.workflow.application.runtime.dispatcher import build_gateway_dispatcher
from backend.workflow.infrastructure.db import Decision, ExecutionRun

logger = structlog.get_logger(__name__)


DECISION_NO_MODEL_ACCOUNT = "no_model_account"
DECISION_AMBIGUOUS_MODEL_ACCOUNT = "ambiguous_model_account"


async def _list_active_workspace_accounts(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[ModelAccount]:
    """All ``is_active`` ModelAccounts for ``workspace_id`` (across accounts).

    Lift I-Repo-Router — delegates to
    :meth:`ModelAccountRepository.list_active_for_workspace` so the run
    resolver depends on the Protocol seam, not on a raw
    raw SQLAlchemy query. The wrapper is kept (rather than inlining the
    Repository at every call-site) because several call-sites in this file
    share the same workspace-scoped fetch + the integer-len fallback logic
    around it."""
    from backend.router.infrastructure.repositories import (  # noqa: PLC0415 — avoid import cycle
        SqlAlchemyModelAccountRepository,
    )

    repo = SqlAlchemyModelAccountRepository(session)
    rows = await repo.list_active_for_workspace(workspace_id=workspace_id)
    return list(rows)


def _single_native_account(accounts: list[ModelAccount]) -> ModelAccount | None:
    """The lone active NON-executor account, or ``None`` when there are zero or
    more than one.

    The cheap-LLM resolvers (frame stage + settle entity extractor) drive a
    native chat model and cannot use a ``provider='executor'`` CLI account. A
    workspace that has registered an executor worker therefore carries the
    native account PLUS one executor account per capability — so a naive
    "exactly one active account" check returns nothing and silently drops these
    stages to their keyword/soft fallback. Filter executor accounts out first,
    then require exactly one native account (never guess among several)."""
    native = [a for a in accounts if not is_executor_account(a)]
    return native[0] if len(native) == 1 else None


async def resolve_workspace_model_account(
    session: AsyncSession, run: ExecutionRun
) -> ModelAccount | None:
    """Resolve the workspace's *active* ModelAccount for this run.

    Phase 2 v1 policy (implemented EXACTLY):

    * exactly one active account → return it.
    * ZERO or MORE-THAN-ONE → do NOT crash, do NOT silently guess: create a
      :class:`~backend.workflow.infrastructure.db.Decision` (so the run is
      paused on a founder decision, staying RUNNING) and return ``None``.
      Honors the founder-in-the-loop invariant — stuck → Decision, never a
      silent stall.
    """
    accounts = await _list_active_workspace_accounts(session, run.workspace_id)
    if len(accounts) == 1:
        return accounts[0]

    if not accounts:
        kind = DECISION_NO_MODEL_ACCOUNT
        reason = "no active model account for workspace"
    else:
        kind = DECISION_AMBIGUOUS_MODEL_ACCOUNT
        reason = f"ambiguous: {len(accounts)} active model accounts"

    session.add(
        Decision(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            decision=kind,
            actor_id=None,
            rationale=reason,
            payload={
                "active_model_account_count": len(accounts),
                "active_model_account_ids": [str(a.id) for a in accounts],
            },
        )
    )
    await session.flush()
    logger.info(
        "worker_run_model_account_unresolved",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
        kind=kind,
        active_count=len(accounts),
    )
    return None


async def _resolve_judge_llm(
    session: AsyncSession, run: ExecutionRun, settings: Settings
) -> GatewayLoopLlm | None:
    """Resolve a judge LLM for the executor verification path (B2b).

    The executor account routes work to an external CLI worker — it cannot grade
    a judge contract itself. So the judge runs on a SEPARATE, NON-executor
    active ModelAccount (an api-llm account in the same workspace), resolved
    here independently of the run's executor account. Mirrors the
    settle-extractor resolution: the FIRST active non-executor account wins;
    ``None`` when the workspace has only executor accounts active — in which
    case a judge-bearing contract routes to a human-review Decision (never a
    silent pass). Command-only contracts still verify without a judge."""
    accounts = await _list_active_workspace_accounts(session, run.workspace_id)
    judge_account = next((a for a in accounts if not is_executor_account(a)), None)
    if judge_account is None:
        logger.info(
            "executor_judge_llm_unresolved",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
        )
        return None
    dispatcher = build_gateway_dispatcher(session, settings)
    return GatewayLoopLlm(
        dispatcher=dispatcher,
        workspace_id=run.workspace_id,
        account_id=judge_account.account_id,
        model_account_id=judge_account.id,
    )


__all__ = [
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
    "_list_active_workspace_accounts",
    "_resolve_judge_llm",
    "_single_native_account",
    "resolve_workspace_model_account",
]
