"""Workspace ModelAccount resolution policy (Lift E2 — resolver-backed).

Three thin helpers shared across the agent / settle / product-bootstrap
runtimes:

* :func:`_list_active_workspace_accounts` — workspace-scoped active
  account fetch.
* :func:`_resolve_via_caller` — pull an adapter for one caller_id via
  :class:`backend.dispatch.resolver.ModelAccountResolver`, returning
  ``None`` on :class:`NoMatchingRouteError` (the runtime branches on
  ``None``: a Decision row in the run worker, a soft-skip in the
  ingest/extract pipeline).
* :func:`_resolve_judge_llm` — judge LLM for the executor verification
  path, resolved via caller_id ``workflow.judge``.

Lift E2 also keeps the historical Decision-marking helper
:func:`resolve_workspace_model_account`. The fallback semantics changed:
when the resolver finds nothing the helper still writes a founder
Decision (so the founder UI surfaces the missing-route condition) and
returns ``None``. The decision kinds stay byte-identical for downstream
consumers.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.dispatch.adapter import ModelAccountAdapter
from backend.dispatch.resolver import (
    ModelAccountResolver,
    NoMatchingRouteError,
    ResolvedAccount,
)
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.predicates import is_executor_account
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.infrastructure.db import Decision, ExecutionRun

logger = structlog.get_logger(__name__)


DECISION_NO_MODEL_ACCOUNT = "no_model_account"
DECISION_AMBIGUOUS_MODEL_ACCOUNT = "ambiguous_model_account"


async def _list_active_workspace_accounts(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[ModelAccount]:
    """All ``is_active`` ModelAccounts for ``workspace_id``."""
    from backend.router.infrastructure.repositories import (  # noqa: PLC0415
        SqlAlchemyModelAccountRepository,
    )

    repo = SqlAlchemyModelAccountRepository(session)
    rows = await repo.list_active_for_workspace(workspace_id=workspace_id)
    return list(rows)


async def _resolve_via_caller(
    session: AsyncSession,
    *,
    caller_id: str,
    workspace_id: uuid.UUID,
    settings: Settings,
    redis: Any = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    run_id: uuid.UUID | None = None,
    repo_url: str | None = None,
) -> ResolvedAccount | None:
    """Resolve ``(caller_id, workspace_id)`` via the resolver.

    Returns ``None`` on
    :class:`~backend.dispatch.resolver.NoMatchingRouteError` /
    ``KeyError`` so call sites can soft-fall-back (a Decision row in the
    run worker; ``None`` short-circuit in the settle / bootstrap paths)
    instead of crashing.

    ``redis`` is threaded into the resolver so an executor adapter has a
    transport for the worker stream XADD (Lift E3). Optional — LiteLLM
    accounts never touch it.

    ``session_factory`` (Lift E19) is threaded into the resolver so the
    ExecutorAdapter can open a fresh ``AsyncSession`` per ``chat`` call.
    Required for any call path that fans out across asyncio tasks (the
    :meth:`IngestCompiler.compile_batch` chunk loop); optional for
    single-flight callers. Without it, parallel chunks race on
    ``session.flush()`` ("Session is already flushing") — the E18 bug.
    """

    async def _build_intent_classifier():
        # Lift N1 — built lazily by the resolver ONLY when a rule keys on
        # classified_intent (semantic category routing). Scoped to the
        # workspace's personal account, where intents + embedding config live.
        from backend.router.accounts.account_service import (  # noqa: PLC0415
            ensure_personal_account,
        )
        from backend.router.routing.run_routing.intent_classifier import (  # noqa: PLC0415
            build_intent_classifier,
        )

        account = await ensure_personal_account(session, workspace_id=workspace_id)
        return await build_intent_classifier(
            session, settings, workspace_id=workspace_id, account_id=account.id
        )

    resolver = ModelAccountResolver(
        session,
        settings=settings,
        redis=redis,
        session_factory=session_factory,
        run_id=run_id,
        repo_url=repo_url,
        intent_classifier_builder=_build_intent_classifier,
    )
    try:
        return await resolver.resolve_for(caller_id=caller_id, workspace_id=workspace_id)
    except NoMatchingRouteError:
        logger.info(
            "resolve_via_caller_no_match",
            caller_id=caller_id,
            workspace_id=str(workspace_id),
        )
        return None
    except KeyError:
        logger.warning(
            "resolve_via_caller_unknown_caller",
            caller_id=caller_id,
            workspace_id=str(workspace_id),
        )
        return None


async def resolve_workspace_model_account(
    session: AsyncSession, run: ExecutionRun
) -> ModelAccount | None:
    """Resolve the workspace's *active* ModelAccount for this run.

    Lift E2 routes through the resolver via caller_id
    ``workflow.agent_loop.act`` first; on miss the legacy
    exactly-one-active-non-executor heuristic remains (kept so a
    workspace with one ModelAccount and no rules still works end-to-end
    without forcing the founder to mint a routing rule for every run).
    Both ZERO and AMBIGUOUS terminal states write the historical
    :class:`Decision` rows so existing founder UIs keep their semantics.
    """
    from backend.config import get_settings  # noqa: PLC0415
    from backend.dispatch.caller_registry import CALLER_AGENT_LOOP_ACT  # noqa: PLC0415

    settings = get_settings()
    resolved = await _resolve_via_caller(
        session,
        caller_id=CALLER_AGENT_LOOP_ACT,
        workspace_id=run.workspace_id,
        settings=settings,
    )
    if resolved is not None:
        return resolved.account

    # Fallback: legacy exactly-one-active heuristic, identical to v1
    # behaviour so existing single-account workspaces are unaffected.
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


def _single_native_account(accounts: list[ModelAccount]) -> ModelAccount | None:
    """The lone active NON-executor account (kept for the legacy soft-fallback
    paths that don't have a caller_id yet — settle / bootstrap callers route
    through :func:`_resolve_via_caller` first, then optionally degrade here).
    """
    native = [a for a in accounts if not is_executor_account(a)]
    return native[0] if len(native) == 1 else None


async def _resolve_judge_llm(
    session: AsyncSession,
    run: ExecutionRun,
    settings: Settings,
    *,
    redis: Any = None,
) -> ResolverLoopLlm | None:
    """Resolve a judge LLM for the executor verification path.

    Routes via caller_id ``workflow.judge``. ``None`` on miss — the judge
    contract then routes to a human-review Decision (never a silent pass).
    """
    from backend.dispatch.caller_registry import CALLER_JUDGE  # noqa: PLC0415

    resolved = await _resolve_via_caller(
        session,
        caller_id=CALLER_JUDGE,
        workspace_id=run.workspace_id,
        settings=settings,
        redis=redis,
    )
    if resolved is None:
        logger.info(
            "executor_judge_llm_unresolved",
            run_id=str(run.id),
            workspace_id=str(run.workspace_id),
        )
        return None
    return ResolverLoopLlm(adapter=resolved.adapter)


def _judge_loop_for_adapter(adapter: ModelAccountAdapter) -> ResolverLoopLlm:
    """Wrap a pre-resolved adapter into a :class:`ResolverLoopLlm` for the judge."""
    return ResolverLoopLlm(adapter=adapter)


__all__ = [
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
    "_judge_loop_for_adapter",
    "_list_active_workspace_accounts",
    "_resolve_judge_llm",
    "_resolve_via_caller",
    "_single_native_account",
    "resolve_workspace_model_account",
]
