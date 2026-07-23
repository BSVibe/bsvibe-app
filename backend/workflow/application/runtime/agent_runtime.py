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
:class:`backend.dispatch.adapter.ExecutorAdapter.chat` — one chat turn at a
time — rather than any full-run wrapper.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.workflow.application.product_tick_planner import ProductTickPlanner

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.dispatch.caller_registry import (
    CALLER_AGENT_LOOP_ACT,
    CALLER_FRAME,
)
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.skill.loader import SkillLoader
from backend.knowledge.retrieval.answer_grounding import build_answer_retriever
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
    get_sandbox_manager,
)
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps

logger = structlog.get_logger(__name__)


async def _product_repo_url(session: AsyncSession, product_id: uuid.UUID) -> str | None:
    """Lift E32 — return the product's git URL for worker-side cloning.

    The agent_loop passes this through ``_resolve_via_caller`` so the
    ExecutorAdapter the resolver hands back tells the worker to clone
    the repo into the per-task workspace. Soft-fails (returns ``None``)
    on a missing product or an empty ``repo_url`` so a substrate-only
    run still resolves an adapter for its non-code chat callers.
    """
    from sqlalchemy import select  # noqa: PLC0415 — keep imports terse at module load

    from backend.identity.workspaces_db import ProductRow  # noqa: PLC0415

    repo_url = (
        await session.execute(select(ProductRow.repo_url).where(ProductRow.id == product_id))
    ).scalar_one_or_none()
    return repo_url or None


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


def _resolve_sandbox_manager(
    sandbox_manager: SandboxManager | None, settings: Settings
) -> SandboxManager:
    """Pick the sandbox backend EXPLICITLY — never a silent host fallback.

    [[bsvibe-no-implicit-routing]]: an injected manager (tests) wins; otherwise
    when ``sandbox_enabled`` the Docker (DinD) manager MUST build — an
    enabled-but-unbuildable sandbox raises rather than degrading to host
    execution (the old ``… or NoopSandboxManager()`` tail silently ran the
    verifier's ``command`` checks as worker-container subprocesses, where the
    project toolchain is absent). Only when the sandbox is *explicitly* disabled
    do we use the host :class:`NoopSandboxManager`.
    """
    if sandbox_manager is not None:
        return sandbox_manager
    if settings.sandbox_enabled:
        built = get_sandbox_manager()
        if built is None:
            raise RuntimeError(
                "sandbox_enabled is true but no sandbox manager could be built — "
                "refusing to silently fall back to host execution"
            )
        return built
    return NoopSandboxManager()


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
    box: SandboxManager = _resolve_sandbox_manager(sandbox_manager, settings)
    skills_root = Path(settings.skills_root)

    def _skill_loader_for(workspace_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(skills_root / str(workspace_id))
        loader.load_all()
        return loader

    async def _retriever_for(
        session: AsyncSession,
        workspace_id: uuid.UUID,
    ) -> CanonRetriever:
        """The workspace-scoped BSage canon retriever, with semantic note search
        folded in when the deployment configures a knowledge embedding model.

        Delegates to the shared :func:`build_canon_retriever` so the in-process loop and the MCP
        transport ground the executor's ``knowledge_search`` identically (INV-7 #1)."""
        from backend.knowledge.retrieval.answer_grounding import (  # noqa: PLC0415
            build_canon_retriever,
        )

        return build_canon_retriever(session, settings=settings, workspace_id=workspace_id)

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

    def _tick_planner_for(session: AsyncSession) -> ProductTickPlanner:
        """Per-run product-tick planner bound to the framing session.

        Threads ``redis_client`` EXACTLY like :func:`_frame_llm_for` so the
        planner resolves ``CALLER_FRAME`` identically to the frame stage. An
        executor-account frame route needs the redis transport for its
        worker-stream XADD; a ``redis=None`` planner would silently fail on such
        a workspace and the tick would degrade to the static meta-instruction
        while every test stayed green (unit-green ≠ prod-works)."""
        # Imported inside the closure: the planner imports account_resolution,
        # which lives in this ``runtime`` package — a module-level import here
        # cycles through ``runtime/__init__`` (mirrors the other lazy imports in
        # this factory, e.g. resolve_workspace_model_account / build_canon_retriever).
        from backend.workflow.application.product_tick_planner import (  # noqa: PLC0415
            ProductTickPlanner,
        )

        return ProductTickPlanner(session, settings=settings, redis=redis_client)

    async def _factory(session: AsyncSession, run: ExecutionRun) -> RunCompute | None:
        """Per-run orchestrator factory — Lift E3 unifies the path.

        Every account (LiteLLM OR executor) routes through
        :class:`RunOrchestrator` (the native BSVibe agent loop). For an
        executor account the resolver hands back an
        :class:`~backend.dispatch.adapter.ExecutorAdapter`, so each
        plan/act/judge turn becomes a single-shot CLI subprocess call
        through the worker; the agent loop, tool set, and verification
        contract are BSVibe's. There is no full-run executor wrapper — the
        bypass is gone (per design ``BSVibe_Dispatch_Redesign_2026-06-05.md``
        §2.1 and Lift E3 invariant in :mod:`backend.dispatch.adapter`).

        On no-match writes the historical ``DECISION_NO_MODEL_ACCOUNT``
        :class:`Decision` so the founder UI surfaces the missing-route
        condition (Lift E1/E2 invariant).
        """
        from backend.workflow.application.runtime.account_resolution import (  # noqa: PLC0415
            resolve_workspace_model_account,
        )

        # Lift E32 — look up the product's repo URL so the worker can
        # clone it into the per-task workspace before invoking the
        # executor. Without it the coding agent gets an empty tempdir
        # and the E31 dogfood symptom returns: 0 file edits, NULL
        # artifact_refs. ``None`` keeps the pre-E32 empty-tempdir path
        # for runs without a product (substrate-only tasks).
        repo_url = await _product_repo_url(session, run.product_id) if run.product_id else None

        # L10 (#5) — Knowledge-only short-circuit (B9b): a frame-classified
        # ``knowledge_only`` ask is a CHAT answer, no engineering work. It MUST
        # use a chat model (``CALLER_FRAME``), NOT the act-stage executor — a
        # coding-agent CLI fails on a chat prompt with "executor chat task …
        # failed: exit 1" (prod symptom, [[bsvibe-executor-subprocess-too-heavy]]).
        # Resolve the chat account BEFORE the act account so a question never
        # touches the executor.
        if _is_knowledge_only(run):
            chat = await _resolve_via_caller(
                session,
                caller_id=CALLER_FRAME,
                workspace_id=run.workspace_id,
                settings=settings,
                redis=redis_client,
            )
            if chat is None:
                # A question with no chat model does NOT become work. Falling
                # through to the act path would hand it to the coding executor —
                # the misroute the frame stage exists to prevent. Decision + pause.
                await resolve_workspace_model_account(session, run)
                logger.info("knowledge_only_chat_unresolved", run_id=str(run.id))
                return None
            logger.info("knowledge_only_route", run_id=str(run.id))
            return KnowledgeAnswerOrchestrator(
                session=session,
                llm=ResolverLoopLlm(adapter=chat.adapter),
                # An ANSWER needs note CONTENT; the verify path's retriever carries
                # only "Related note — <path>" pointers. Same builder as the inline
                # /ask service, so both surfaces ground identically.
                retriever=build_answer_retriever(
                    session, settings=settings, workspace_id=run.workspace_id
                ),
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
            # Lift E32 — thread the product's repo URL so the worker
            # clones it into the per-task workspace.
            repo_url=repo_url,
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
        tick_planner_for=_tick_planner_for,
    )


__all__ = [
    "_build_composite_workspace_provisioner",
    "_frame_skill_hint",
    "_is_knowledge_only",
    "_product_workspace_provisioner",
    "build_agent_execution_deps",
]
