"""Production :class:`AgentExecutionDeps` factory (Lift E3 — adapter-only).

Constructs the run-orchestrator factory + skill-loader factory + frame-LLM
factory + composite workspace provisioner that the
:class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker`
threads through every plan/act/judge turn.

After Lift E2 every LLM call site (frame / plan / act / judge / settle /
bootstrap) flows through
:class:`backend.dispatch.resolver.ModelAccountResolver` keyed on a
``caller_id``. No classifier, no tier, no provider allow-list.

After Lift E3 the executor-account bypass is GONE — every account, whether
LiteLLM or executor, routes through :class:`RunOrchestrator` (the native
BSVibe agent loop), and the executor's CLI subprocess is reached via
:class:`backend.dispatch.adapter.ExecutorAdapter.chat` instead of the legacy
:class:`backend.executors.coordinator.ExecutorOrchestrator` full-run wrapper.
``ExecutorOrchestrator`` is still importable for the integration tests that
construct it directly, but the runtime factory no longer reaches for it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.dispatch.caller_registry import (
    CALLER_AGENT_LOOP_ACT,
    CALLER_FRAME,
)
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.skill.loader import SkillLoader
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.workflow.application.agent_loop import (
    CanonRetriever,
    RunCompute,
    RunOrchestrator,
)
from backend.workflow.application.delivery.connector_dispatch import (
    build_github_workspace_provisioner,
)
from backend.workflow.application.knowledge_orchestrator import KnowledgeAnswerOrchestrator
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import (
    _resolve_via_caller,
)
from backend.workflow.application.runtime.dispatcher import _ResolverFrameLlm
from backend.workflow.application.stages.frame import FrameLlm
from backend.workflow.infrastructure.connector_actions import ConnectorActionResolver
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import (
    NoopSandboxManager,
    SandboxManager,
    build_sandbox_manager,
)
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps

logger = structlog.get_logger(__name__)


async def _product_workspace_provisioner(
    session: AsyncSession,
    run: ExecutionRun,
    workspace_dir: Path,
) -> bool:
    """W1: provision the run's workspace_dir as a git worktree of the
    product's main branch. Lazily initialises the product workspace if
    missing.
    """
    if run.product_id is None:
        return False

    from backend.storage.product_workspace import (  # noqa: PLC0415 — lazy
        add_run_worktree,
        init_product_workspace,
    )

    await init_product_workspace(run.product_id)
    if workspace_dir.exists() and not any(workspace_dir.iterdir()):  # noqa: ASYNC240
        workspace_dir.rmdir()  # noqa: ASYNC240
    await add_run_worktree(run.product_id, run.id)
    return True


def _build_composite_workspace_provisioner(
    *,
    github: Callable[[AsyncSession, ExecutionRun, Path], Awaitable[None]],
    product: Callable[[AsyncSession, ExecutionRun, Path], Awaitable[bool]],
) -> Callable[[AsyncSession, ExecutionRun, Path], Awaitable[None]]:
    """Compose the two W1 provisioners in priority order."""

    async def _composed(session: AsyncSession, run: ExecutionRun, workspace_dir: Path) -> None:
        await github(session, run, workspace_dir)
        if not workspace_dir.exists() or any(workspace_dir.iterdir()):  # noqa: ASYNC240
            return
        await product(session, run, workspace_dir)

    return _composed


def _is_knowledge_only(run: ExecutionRun) -> bool:
    """Read the frame's ``path_classification`` off ``run.payload`` (B9b)."""
    payload = run.payload or {}
    frame = payload.get("frame") if isinstance(payload, dict) else None
    classification = frame.get("path_classification") if isinstance(frame, dict) else None
    return classification == "knowledge_only"


def _frame_skill_hint(
    run: ExecutionRun, skill_loader_for: Callable[[uuid.UUID], SkillLoader]
) -> tuple[str | None, str | None]:
    """Read the frame's matched skill off ``run.payload`` + resolve its description."""
    payload = run.payload or {}
    frame = payload.get("frame") if isinstance(payload, dict) else None
    skill_match = frame.get("skill_match") if isinstance(frame, dict) else None
    if not isinstance(skill_match, str) or not skill_match:
        return None, None
    loader = skill_loader_for(run.workspace_id)
    meta = loader.registry.get(skill_match)
    description = meta.description if meta is not None else None
    return skill_match, description


def build_agent_execution_deps(
    *,
    settings: Settings | None = None,
    sandbox_manager: SandboxManager | None = None,
    redis_client: Any = None,
    connector_plugins: dict[str, PluginMeta] | None = None,
) -> AgentExecutionDeps:
    """The production execution backend for :class:`AgentWorker`.

    * work-LLM = :class:`ResolverLoopLlm` over the adapter the dispatch
      resolver returned for caller_id ``workflow.agent_loop.act``.
    * frame-LLM = :class:`_ResolverFrameLlm` over caller_id
      ``workflow.frame``. ``None`` (no rule + no workspace default) →
      keyword-fallback frame.
    * sandbox / skill_loader / provisioner / redis wiring unchanged.
    """
    settings = settings or get_settings()
    box: SandboxManager = sandbox_manager or build_sandbox_manager() or NoopSandboxManager()
    skills_root = Path(settings.skills_root)
    knowledge_vault_root = Path(settings.knowledge_vault_root)

    def _skill_loader_for(workspace_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(skills_root / str(workspace_id))
        loader.load_all()
        return loader

    async def _retriever_for(
        session: AsyncSession,
        workspace_id: uuid.UUID,
    ) -> CanonRetriever:
        """The workspace-scoped BSage canon retriever, with semantic note search
        folded in when the deployment configures a knowledge embedding model."""
        from backend.knowledge.factory import (  # noqa: PLC0415 — lazy heavy import
            KnowledgeFactory,
        )
        from backend.knowledge.retrieval.composite_retriever import (  # noqa: PLC0415
            CompositeCanonRetriever,
        )
        from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
            resolve_knowledge_embedder,
        )
        from backend.knowledge.retrieval.semantic_note_retriever import (  # noqa: PLC0415
            SemanticNoteRetriever,
        )
        from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend  # noqa: PLC0415

        base = KnowledgeFactory(
            region=settings.knowledge_default_region,
            workspace_id=str(workspace_id),
            vault_root=knowledge_vault_root,
        ).retriever()
        embedder = resolve_knowledge_embedder(settings)
        if not embedder.enabled or embedder.model is None:
            return base
        semantic = SemanticNoteRetriever(
            embedder,
            PgNoteVectorBackend(session, workspace_id=workspace_id, embedding_model=embedder.model),
        )
        return CompositeCanonRetriever([base, semantic])

    async def _frame_llm_for(session: AsyncSession, workspace_id: uuid.UUID) -> FrameLlm | None:
        """Per-workspace cheap-LLM for the frame stage via the resolver.

        ``None`` (no rule + no workspace default) → keyword-fallback in
        the frame stage.
        """
        resolved = await _resolve_via_caller(
            session,
            caller_id=CALLER_FRAME,
            workspace_id=workspace_id,
            settings=settings,
            redis=redis_client,
        )
        if resolved is None:
            logger.info(
                "frame_llm_account_unresolved",
                workspace_id=str(workspace_id),
                caller_id=CALLER_FRAME,
            )
            return None
        return _ResolverFrameLlm(adapter=resolved.adapter)

    async def _factory(session: AsyncSession, run: ExecutionRun) -> RunCompute | None:
        """Per-run orchestrator factory — Lift E3 unifies the path.

        Every account (LiteLLM OR executor) routes through
        :class:`RunOrchestrator` (the native BSVibe agent loop). For an
        executor account the resolver hands back an
        :class:`~backend.dispatch.adapter.ExecutorAdapter`, so each
        plan/act/judge turn becomes a single-shot CLI subprocess call
        through the worker; the agent loop, tool set, and verification
        contract are BSVibe's. The legacy
        :class:`~backend.executors.coordinator.ExecutorOrchestrator`
        full-run wrapper is no longer reached from this factory (the
        bypass is gone — per design ``BSVibe_Dispatch_Redesign_2026-06-05.md``
        §2.1 and Lift E3 invariant in :mod:`backend.dispatch.adapter`).

        On no-match writes the historical ``DECISION_NO_MODEL_ACCOUNT``
        :class:`Decision` so the founder UI surfaces the missing-route
        condition (Lift E1/E2 invariant).
        """
        from backend.workflow.application.runtime.account_resolution import (  # noqa: PLC0415
            resolve_workspace_model_account,
        )

        resolved = await _resolve_via_caller(
            session,
            caller_id=CALLER_AGENT_LOOP_ACT,
            workspace_id=run.workspace_id,
            settings=settings,
            redis=redis_client,
            # Lift E31 — thread the run id so the ExecutorAdapter binds
            # its dispatched task to the run for artifact persistence
            # (files captured by the worker → run's ``artifact_refs``).
            run_id=run.id,
        )
        if resolved is None:
            # Fallthrough writes a Decision when there's truly no LLM
            # for the workspace — preserves the existing founder UX.
            await resolve_workspace_model_account(session, run)
            logger.info(
                "agent_runtime_account_unresolved",
                run_id=str(run.id),
                workspace_id=str(run.workspace_id),
                caller_id=CALLER_AGENT_LOOP_ACT,
            )
            return None

        retriever = await _retriever_for(session, run.workspace_id)
        suggested_skill, suggested_skill_description = _frame_skill_hint(run, _skill_loader_for)

        llm = ResolverLoopLlm(adapter=resolved.adapter)

        # Knowledge-only short-circuit (B9b): a frame-classified
        # ``knowledge_only`` ask answers from the BSage ontology.
        if _is_knowledge_only(run):
            logger.info(
                "knowledge_only_route",
                run_id=str(run.id),
                workspace_id=str(run.workspace_id),
            )
            return KnowledgeAnswerOrchestrator(
                session=session,
                llm=llm,
                retriever=retriever,
            )

        skill_loader = _skill_loader_for(run.workspace_id)
        connector_actions = (
            ConnectorActionResolver(
                session=session,
                plugins_by_name=connector_plugins,
                cipher=CredentialCipher(_key_from_settings()),
            )
            if connector_plugins
            else None
        )
        return RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=box,
            retriever=retriever,
            skill_loader=skill_loader,
            connector_actions=connector_actions,
            redis_client=redis_client,
            settings=settings,
            suggested_skill=suggested_skill,
            suggested_skill_description=suggested_skill_description,
        )

    github_provisioner = build_github_workspace_provisioner(
        cipher=lambda: CredentialCipher(_key_from_settings())
    )
    provisioner = _build_composite_workspace_provisioner(
        github=github_provisioner,
        product=_product_workspace_provisioner,
    )

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=_factory,
        workspace_root=Path(settings.run_workspace_root),
        workspace_provisioner=provisioner,
        frame_llm=_frame_llm_for,
    )


__all__ = [
    "_build_composite_workspace_provisioner",
    "_frame_skill_hint",
    "_is_knowledge_only",
    "_product_workspace_provisioner",
    "build_agent_execution_deps",
]
