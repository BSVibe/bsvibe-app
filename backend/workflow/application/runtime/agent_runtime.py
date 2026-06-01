"""Production :class:`AgentExecutionDeps` factory (§17.2a slice).

Constructs the run-orchestrator factory + skill-loader factory + frame-LLM
factory + composite workspace provisioner that the
:class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker`
threads through every plan/act/judge turn.

Extracted out of the legacy ``backend.workflow.infrastructure.workers.run``
god-file. The frame consumers (``_is_knowledge_only`` + ``_frame_skill_hint``)
sit here too because they only feed the factory.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.skill.loader import SkillLoader
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.dispatch.strategies import is_executor_account
from backend.router.routing.run_routing import resolve_route
from backend.workflow.application.agent_loop import (
    CanonRetriever,
    RunCompute,
    RunOrchestrator,
)
from backend.workflow.application.delivery.connector_dispatch import (
    build_github_workspace_provisioner,
)
from backend.workflow.application.knowledge_orchestrator import KnowledgeAnswerOrchestrator
from backend.workflow.application.loop_llm import GatewayLoopLlm
from backend.workflow.application.runtime.account_resolution import (
    _list_active_workspace_accounts,
    _resolve_judge_llm,
    _single_native_account,
)
from backend.workflow.application.runtime.dispatcher import (
    _GatewayFrameLlm,
    build_gateway_dispatcher,
)
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
    missing (the startup hook for backfill — keeps the lift simple).

    Returns ``True`` when a worktree was provisioned (so the composite
    provisioner knows the slot is taken); ``False`` when this run has no
    product_id and the empty scratch dir should stand (legacy / test
    behavior). A raised :class:`ProductWorkspaceError` surfaces to
    AgentWorker, which marks the run terminal with a usable reason.

    The empty ``workspace_dir`` created by AgentWorker is REMOVED before
    ``git worktree add`` (git refuses to write into an existing dir).
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
    """Compose the two W1 provisioners in priority order:

    1. github connector binding → existing clone path
    2. product workspace (no github binding) → new git worktree
    3. neither → leave scratch dir empty (legacy)

    The github provisioner is a no-op on no-binding (silent return), so
    we detect "did github do something" by checking whether the dir is
    still empty after it ran. If yes, try product. If product also
    returns False, the empty dir stays — matching pre-W1 behavior for
    tests that inject neither binding nor product.
    """

    async def _composed(session: AsyncSession, run: ExecutionRun, workspace_dir: Path) -> None:
        await github(session, run, workspace_dir)
        # github provisioner removes the empty dir + clones into it. If the
        # dir is now missing OR non-empty, github filled it — done.
        if not workspace_dir.exists() or any(workspace_dir.iterdir()):  # noqa: ASYNC240
            return
        # Empty dir + no github → product workspace if available.
        await product(session, run, workspace_dir)

    return _composed


def _is_knowledge_only(run: ExecutionRun) -> bool:
    """Read the frame's ``path_classification`` off ``run.payload`` (B9b).

    The :class:`AgentWorker` records the full frame onto ``run.payload["frame"]``
    BEFORE the orchestrator factory runs, so the knowledge-only branch is
    available here. ``True`` only when the frame explicitly classified the ask
    ``knowledge_only`` — any other value, a missing frame, or a malformed
    payload is the agent-loop default (no strand)."""
    payload = run.payload or {}
    frame = payload.get("frame") if isinstance(payload, dict) else None
    classification = frame.get("path_classification") if isinstance(frame, dict) else None
    return classification == "knowledge_only"


def _frame_skill_hint(
    run: ExecutionRun, skill_loader_for: Callable[[uuid.UUID], SkillLoader]
) -> tuple[str | None, str | None]:
    """Read the frame's matched skill off ``run.payload`` + resolve its description.

    The :class:`AgentWorker` records the frame onto ``run.payload["frame"]``
    BEFORE the orchestrator factory runs, so the matched skill is available
    here to thread into the loop as a first-invocation hint (B9a — the frame
    output is finally consumed). The description is looked up in the SAME
    per-workspace skill loader the frame matched against. No frame / no match
    / a stale name not in the loader → ``(None, None)`` (no hint)."""
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

    * work-LLM = :class:`GatewayLoopLlm` over a per-session
      :class:`GatewayDispatcher` (same build as the HTTP chat path), bound to
      the run's resolved workspace ModelAccount.
    * sandbox = the resolved :class:`SandboxManager` —
      :class:`DockerSandboxManager` when ``sandbox_enabled``, else
      :class:`NoopSandboxManager` so dev runs without Docker (the orchestrator
      requires a non-None manager).
    * skill_loader_for = a per-workspace factory ``workspace_id ->
      SkillLoader`` rooted at ``<skills_root>/<workspace_id>/`` (Workflow §6
      #5 — skills are per-workspace). The returned loader is already
      ``load_all()``-ed so :class:`FrameStage` frames against that workspace's
      skills only, never a single shared root-level set.
    * run workspace = ``run_workspace_root/<run_id>`` (per
      :meth:`AgentWorker._frame_and_drive`).

    ``sandbox_manager`` may be injected (tests pass a Noop manager / CI runs
    without Docker); otherwise it is resolved from settings.

    ``redis_client`` (only set in ``worker_mode="redis_streams"``) is threaded
    into each per-run :class:`RunOrchestrator` so the verified terminal emits
    the ``deliver`` + ``settle`` wake-up notifications. ``None`` (the default)
    keeps the pure DB-polling behaviour — the orchestrator emits nothing.

    ``connector_plugins`` (B5b) is the loaded plugin registry. When provided,
    each per-run native :class:`RunOrchestrator` is given a
    :class:`ConnectorActionResolver` so the work LLM can take the workspace's
    ``mcp_exposed`` connector actions mid-run. ``None`` (the default, every
    existing caller/test) surfaces no connector tools — zero behaviour change.
    The registry is loaded once at process start (``run_workers``) and shared
    across runs. Lift 0c retired the load-time danger verdict map.
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
        """The workspace-scoped BSage canon retriever (B3 / RC-2 fix), with G5
        semantic note search folded in when the deployment configures a knowledge
        embedding model."""
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
        """B9a — the per-workspace cheap-LLM for the frame stage. Returns
        ``None`` (keyword fallback) for zero / ambiguous / executor-only
        workspaces."""
        accounts = await _list_active_workspace_accounts(session, workspace_id)
        account = _single_native_account(accounts)
        if account is None:
            logger.info(
                "frame_llm_account_unresolved",
                workspace_id=str(workspace_id),
                active_count=len(accounts),
            )
            return None
        dispatcher = build_gateway_dispatcher(session, settings)
        return _GatewayFrameLlm(
            dispatcher=dispatcher,
            workspace_id=workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )

    async def _factory(session: AsyncSession, run: ExecutionRun) -> RunCompute | None:
        # Phase 1: rule-based routing picks the account from the run's framed
        # signals when the workspace has routing rules; otherwise it delegates
        # to the legacy single-active resolver (zero behaviour change).
        account = await resolve_route(session, run)
        if account is None:
            return None
        retriever = await _retriever_for(session, run.workspace_id)
        # B9a — CONSUME the frame: the worker recorded ``run.payload["frame"]``
        # (skill match by description) BEFORE this factory runs.
        suggested_skill, suggested_skill_description = _frame_skill_hint(run, _skill_loader_for)
        # Executor-pool Lift 5b: a ``provider='executor'`` account routes to a
        # registered external CLI worker, NOT the native LLM loop.
        if is_executor_account(account):
            verify_llm = await _resolve_judge_llm(session, run, settings)
            return ExecutorOrchestrator(
                session=session,
                redis=redis_client,
                account=account,
                sandbox_manager=box,
                settings=settings,
                retriever=retriever,
                verify_llm=verify_llm,
            )
        dispatcher = build_gateway_dispatcher(session, settings)
        llm = GatewayLoopLlm(
            dispatcher=dispatcher,
            workspace_id=run.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )
        # B9b — knowledge-only short-circuit (the cost saver). When the frame
        # classified the ask as ``knowledge_only`` (a question answerable from
        # the workspace's BSage ontology), route to the
        # KnowledgeAnswerOrchestrator — ONE LLM call, no plan→act→verify loop.
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
        # B5a — thread the run's workspace SkillLoader into the native loop so
        # it registers the ``invoke_skill`` + ``knowledge_search`` tools.
        skill_loader = _skill_loader_for(run.workspace_id)
        # B5b — connector-action provider. When the worker loaded the plugin
        # registry (``run_workers`` does), the native loop can take the
        # workspace's ``mcp_exposed`` connector actions mid-run.
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

    # W1: composed workspace provisioner — first try github (clones target
    # repo onto a per-run branch when a binding exists), then fall back to
    # the product workspace path (git worktree from product's main branch
    # when run.product_id is set), else leave the empty scratch dir.
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
        # B9a — the cheap-LLM framing seam, resolved per-workspace via the
        # gateway (mirrors the settle extractor). FrameStage uses it for real
        # framing; None (zero/ambiguous/executor-only account) → keyword
        # fallback.
        frame_llm=_frame_llm_for,
    )


__all__ = [
    "_build_composite_workspace_provisioner",
    "_frame_skill_hint",
    "_is_knowledge_only",
    "_product_workspace_provisioner",
    "build_agent_execution_deps",
]
