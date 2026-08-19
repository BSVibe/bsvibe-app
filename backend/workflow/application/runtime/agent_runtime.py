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
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.workflow.application.product_tick_planner import ProductTickPlanner

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from backend.workflow.application.loop_llm import ResolverLoopLlm
from backend.workflow.application.runtime.account_resolution import (
    product_dispatch_config,
    resolve_via_caller,
)
from backend.workflow.application.runtime.dispatcher import _ResolverFrameLlm
from backend.workflow.application.runtime.sandbox_selection import (
    resolve_sandbox_manager,
    sandbox_manager_for_run,
)
from backend.workflow.application.runtime.workspace_provisioning import (
    _build_composite_workspace_provisioner,
    _product_workspace_provisioner,
)
from backend.workflow.application.stages.frame import FrameLlm
from backend.workflow.domain.client_worktree import client_run_worktree
from backend.workflow.infrastructure.connector_actions import ConnectorActionResolver
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.infrastructure.sandbox import SandboxManager
from backend.workflow.infrastructure.workers.agent_worker import AgentExecutionDeps

logger = structlog.get_logger(__name__)


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
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AgentExecutionDeps:
    """The production execution backend for :class:`AgentWorker`.

    * work-LLM = :class:`ResolverLoopLlm` over the adapter the dispatch
      resolver returned for caller_id ``workflow.agent_loop.act``.
    * frame-LLM = :class:`_ResolverFrameLlm` over caller_id
      ``workflow.frame``. ``None`` (no rule + no workspace default) →
      keyword-fallback frame.
    * sandbox / skill_loader / provisioner / redis wiring unchanged.
    * ``session_factory`` → the act ExecutorAdapter's own connection-free session.
    """
    settings = settings or get_settings()
    box: SandboxManager = resolve_sandbox_manager(sandbox_manager, settings)
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
        resolved = await resolve_via_caller(
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
        # clone it into the per-task workspace before invoking the executor.
        # Without it the coding agent gets an empty tempdir and the E31 dogfood
        # symptom returns: 0 file edits, NULL artifact_refs. ``None`` keeps the
        # pre-E32 empty-tempdir path for runs without a product.
        # E32 (repo_url) + #692 (execution model + client_attach dir): all from
        # the PRODUCT (config lives on the product; the worker holds none).
        # Defaults for a run with no product keep today's behaviour.
        repo_url, execution_target, client_workspace_dir = (
            await product_dispatch_config(session, run.product_id)
            if run.product_id
            else (None, "server_sandbox", None)
        )
        # A client_attach run works in a git worktree of its own under that
        # directory. The directory itself is provisioned at sandbox acquire,
        # which already precedes the first agent turn.
        agent_workspace_dir = (
            client_run_worktree(client_workspace_dir, run.id)
            if execution_target == "client_attach" and client_workspace_dir
            else client_workspace_dir
        )

        # An ASK no longer gets a SEPARATE, TOOL-LESS orchestrator. That
        # short-circuit (B9b) answered from the ontology alone because it never
        # took the tool-surface seam (``tool_registry.mcp_tool_names_for`` /
        # ``RUN_TOOL_FORWARDING``, INV-7) — so prod ``c40c513d``, asked to
        # "코드로 확인하고 근거 파일:라인을 대라", could not open a single file and
        # GUESSED: its own deliverable opens with "코드를 직접 열람한 것이 아님을
        # 먼저 밝힙니다". A starved agent does not fail; it invents.
        #
        # Withholding tools was never the right lever, because whether a question
        # needs the repo is not knowable before the work — the agent finds out by
        # looking. So every run takes this one seam and gets the same surface.
        # What separates an answer from a code change is the ASK directive seeded
        # into the loop (``_loop_context.ask_directive_message``), which also
        # guards the OPPOSITE prod failure: ``ff1615e8`` ("현 프로젝트 상황
        # 설명해줘") reached the loop and SHIPPED an unrelated diff.
        #
        # The cost saver survives on its own — an agent handed tools it does not
        # need stops after one turn. Starving it was never what made it cheap.

        resolved = await resolve_via_caller(
            session,
            caller_id=CALLER_AGENT_LOOP_ACT,
            workspace_id=run.workspace_id,
            settings=settings,
            redis=redis_client,
            session_factory=session_factory,  # drive-session-release: own short session
            # Lift E31 — thread the run id so the ExecutorAdapter binds
            # its dispatched task to the run for artifact persistence
            # (files captured by the worker → run's ``artifact_refs``).
            run_id=run.id,
            # Lift E32 — thread the product's repo URL so the worker
            # clones it into the per-task workspace.
            repo_url=repo_url,
            # #692 — thread the product's execution model + local dir so the
            # dispatched task tells the pure worker WHERE/HOW to run.
            execution_target=execution_target,
            # The run's OWN worktree, not the founder's checkout — the CLI runs
            # with this as its cwd. Derived (not threaded) so this and the
            # verification box cannot drift onto different trees.
            client_workspace_dir=agent_workspace_dir,
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
        # #692 in-place verify — a client_attach run's source is ONLY on the
        # founder's machine, so its gate commands must run there. Picked per run;
        # every other run keeps the process-wide ``box``.
        run_box = sandbox_manager_for_run(
            run_id=run.id,
            default=box,
            execution_target=execution_target,
            client_workspace_dir=client_workspace_dir,
            account=resolved.account,
            redis_client=redis_client,
            session_factory=session_factory,
            workspace_id=run.workspace_id,
            timeout_s=settings.verify_gate_command_timeout_s,
        )
        return RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=run_box,
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
    "_frame_skill_hint",
    "_is_knowledge_only",
    "build_agent_execution_deps",
]
