"""Production worker runtime — the daemon that actually *runs* the workers.

Workflow §11.1 / §12.5 #8 (Bundle G). Phase 1 wired the Direct path end to
end and proved it with ``tests/glue/test_direct_path_e2e.py`` (single-tick
methods, in a test). But nothing in production *runs* the DB-polling workers
or injects real execution dependencies — so a real founder POST landed a
TriggerEvent and then nothing drove it.

This module stands up the production runtime:

* :func:`build_agent_execution_deps` — the real
  :class:`~backend.workflow.infrastructure.workers.agent_worker.AgentExecutionDeps`: the gateway
  work-LLM (built the same way ``backend.api.v1.chat`` builds its
  dispatcher), the real (or Noop) sandbox manager, the workspace skill
  loader, and a per-run orchestrator factory that resolves the run's
  workspace ModelAccount.
* :func:`resolve_workspace_model_account` — the Phase 2 v1 resolution
  policy (exactly one active account → use it; zero / many → create a
  :class:`~backend.workflow.infrastructure.db.Decision`, leave the run RUNNING — never a
  silent guess or stall).
* :class:`RealPluginDispatchAdapter` — bridges the worker's
  :class:`~backend.workflow.infrastructure.workers.delivery_worker.PluginDispatchAdapter` Protocol to
  the real :class:`~backend.workflow.application.delivery.dispatcher.DeliveryDispatcher` over the
  plugins discovered by :class:`~backend.extensions.plugin.loader.PluginLoader`.
* :class:`WorkerRuntime` / :func:`run_workers` — construct + concurrently run
  every worker with a shared session factory and graceful SIGINT/SIGTERM
  shutdown (reusing each worker's :meth:`BaseWorker.start` / ``stop`` —
  the poll loop is not reinvented here).

DB-polling, not Redis Streams (Phase 1 invariant retained).
"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis.asyncio as redis_aio
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import Settings, get_settings
from backend.executors.orchestrator import ExecutorOrchestrator
from backend.extensions.plugin.base import PluginMeta
from backend.extensions.plugin.loader import PluginLoader
from backend.extensions.plugin.runner import PluginRunner
from backend.extensions.skill.loader import SkillLoader
from backend.knowledge.infrastructure.workers.settle_worker import (
    EntityExtractor,
    ExtractorFactory,
    KnowledgeSettleSink,
    NoteEmbedHook,
    Settlement,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)
from backend.router.accounts.crypto import CredentialCipher, _key_from_settings
from backend.router.accounts.models import ModelAccount
from backend.router.accounts.service import ModelAccountService
from backend.router.budget.policy import BudgetPolicyService
from backend.router.budget.repository import BudgetPolicyRepository
from backend.router.budget.tracker import BudgetTracker, InMemoryBudgetStore
from backend.router.classifier.base import ClassificationFeatures
from backend.router.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.router.classifier.static import StaticClassifier
from backend.router.dispatch import DispatchRequest, GatewayDispatcher
from backend.router.dispatch.strategies import EXECUTOR_PROVIDER, is_executor_account
from backend.router.llm_client import LlmClient
from backend.router.routing.run_routing import resolve_route
from backend.schedule.infrastructure.db_poll_runner import build_db_poll_schedule_runner
from backend.schedule.infrastructure.workers.schedule_worker import (
    ScheduleWorker,
    ScheduleWorkerConfig,
)
from backend.workers.base import BaseWorker
from backend.workers.emit import STREAM_AGENT, STREAM_DELIVER, STREAM_INTAKE, STREAM_SETTLE
from backend.workers.relays import build_relay
from backend.workers.streams import RedisStreamConsumer, StreamHandler
from backend.workflow.application.agent_loop import (
    CanonRetriever,
    RunCompute,
    RunOrchestrator,
)
from backend.workflow.application.delivery.connector_dispatch import (
    ConnectorDeliveryAdapter,
    build_connector_delivery_adapter,
    build_github_workspace_provisioner,
)
from backend.workflow.application.delivery.dispatcher import DeliveryDispatcher
from backend.workflow.application.knowledge_orchestrator import KnowledgeAnswerOrchestrator
from backend.workflow.application.loop_llm import GatewayLoopLlm
from backend.workflow.application.safe_mode_expiry import SafeModeExpirySweepRunner
from backend.workflow.application.stages.frame import FrameLlm
from backend.workflow.domain.delivery import DeliveryResult
from backend.workflow.infrastructure.connector_actions import ConnectorActionResolver
from backend.workflow.infrastructure.db import Decision, ExecutionRun
from backend.workflow.infrastructure.sandbox import (
    NoopSandboxManager,
    SandboxManager,
    build_sandbox_manager,
)
from backend.workflow.infrastructure.workers.agent_worker import (
    AgentExecutionDeps,
    AgentWorker,
)
from backend.workflow.infrastructure.workers.delivery_worker import (
    DeliveryWorker,
    PluginDispatchAdapter,
)
from backend.workflow.infrastructure.workers.intake_worker import IntakeWorker
from backend.workflow.infrastructure.workers.relay_worker import RelayWorker
from plugin.audit.models import AuditOutboxRecord

logger = structlog.get_logger(__name__)

# Default plugin-implementations directory (scanned at module import, in sync
# context, so the async loader path stays free of filesystem-resolve calls).
#
# Lift R1 (v8 §D38): connector plugins live at repo-root ``plugin/<name>/``,
# not under ``backend/extensions/implementations/`` (which now holds only the
# yet-to-be-relocated audit plugin pending Lift R2's EventBus rewire).
# Resolution: from ``backend/workflow/infrastructure/workers/run.py`` walk up
# to the repo root (5 parents: workers → infrastructure → workflow → backend
# → repo root) and point at ``<repo_root>/plugin``. ``settings.plugins_dir``
# overrides for tests / non-standard deploy layouts.
_PLUGINS_IMPLEMENTATIONS_DIR = Path(__file__).resolve().parents[4] / "plugin"


# ---------------------------------------------------------------------------
# Gateway work-LLM dispatcher — mirror of backend.api.v1.chat._build_dispatcher
# ---------------------------------------------------------------------------


def build_gateway_dispatcher(session: AsyncSession, settings: Settings) -> GatewayDispatcher:
    """Construct a :class:`GatewayDispatcher` exactly as the HTTP chat path does.

    The work-LLM (:class:`GatewayLoopLlm`) routes every plan/act/judge turn
    through this dispatcher; it resolves the account + model + budget and hands
    off to :class:`LlmClient`. Built per-session so compute shares the run's
    transaction. (Mirrors ``backend.api.v1.chat._build_dispatcher`` —
    intentionally NOT factored out across the HTTP/worker boundary to keep each
    entrypoint's wiring explicit.)"""
    cipher = CredentialCipher(_key_from_settings())
    accounts = ModelAccountService(session, cipher=cipher)
    budget_repo = BudgetPolicyRepository(session)
    tracker = BudgetTracker(InMemoryBudgetStore())
    budget = BudgetPolicyService(repository=budget_repo, tracker=tracker)
    classifier = LocalVsCloudClassifier(
        local_score_max=settings.gateway_local_score_max,
        cloud_score_min=settings.gateway_cloud_score_min,
        static=StaticClassifier(
            local_score_max=settings.gateway_local_score_max,
            cloud_score_min=settings.gateway_cloud_score_min,
        ),
    )
    llm = LlmClient()
    return GatewayDispatcher(accounts=accounts, classifier=classifier, budget=budget, llm=llm)


# ---------------------------------------------------------------------------
# Per-run model-account resolution policy (Phase 2 v1)
# ---------------------------------------------------------------------------

DECISION_NO_MODEL_ACCOUNT = "no_model_account"
DECISION_AMBIGUOUS_MODEL_ACCOUNT = "ambiguous_model_account"


async def _list_active_workspace_accounts(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[ModelAccount]:
    """All ``is_active`` ModelAccounts for ``workspace_id`` (across accounts).

    The :class:`ModelAccountRepository` scopes by ``(workspace_id, account_id)``
    — too narrow here: a run carries only ``workspace_id``, so resolution must
    look across every account in the workspace."""
    stmt = (
        select(ModelAccount)
        .where(
            ModelAccount.workspace_id == workspace_id,
            ModelAccount.is_active.is_(True),
        )
        .order_by(ModelAccount.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


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
      :class:`~backend.workflow.infrastructure.db.Decision` (so the run is paused on a
      founder decision, staying RUNNING) and return ``None``. Honors the
      founder-in-the-loop invariant — stuck → Decision, never a silent stall.
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
    a judge contract itself. So the judge runs on a SEPARATE, NON-executor active
    ModelAccount (an api-llm account in the same workspace), resolved here
    independently of the run's executor account. Mirrors the settle-extractor
    resolution (:func:`build_settle_entity_extractor_factory`): the FIRST active
    non-executor account wins; ``None`` when the workspace has only executor
    accounts active — in which case a judge-bearing contract routes to a
    human-review Decision (never a silent pass). Command-only contracts still
    verify without a judge.
    """
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


# ---------------------------------------------------------------------------
# Production AgentExecutionDeps
# ---------------------------------------------------------------------------


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
    * sandbox = the resolved :class:`SandboxManager` — :class:`DockerSandboxManager`
      when ``sandbox_enabled``, else :class:`NoopSandboxManager` so dev runs
      without Docker (the orchestrator requires a non-None manager).
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
        embedding model.

        Built per run from :class:`KnowledgeFactory` rooted at the SAME
        ``<vault_root>/<region>/<workspace_id>/`` boundary the settle/promotion
        pipeline writes to, so verify folds in THIS workspace's promoted
        canonical patterns. An empty-knowledge workspace yields ``[]`` and the
        retriever never raises into the verify path.

        G6: the pgvector note index is the DERIVED search index of the Markdown
        SoT (proposal §5.4), so semantic search is on whenever
        ``settings.knowledge_embedding_model`` is set — a deployment knob, NOT a
        per-account opt-in. When unset, the base canon-only retriever is
        returned (pre-G5 behaviour). Postgres-only; SemanticNoteRetriever
        degrades to [] on a non-PG dev DB."""
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
        """B9a — the per-workspace cheap-LLM for the frame stage.

        Resolves the workspace's single active ModelAccount and adapts a
        :class:`GatewayDispatcher` (bound to the worker's framing ``session``, so
        it shares the run's transaction) to the :class:`FrameLlm` seam — mirrors
        :func:`build_settle_entity_extractor_factory`'s resolution. Returns
        ``None`` — the keyword fallback — when the workspace has zero or
        more-than-one active account, or only executor accounts are active, so
        framing never guesses a model (and an executor-only / accountless
        workspace keeps the pre-B9a behaviour)."""
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
        # (skill match by description) BEFORE this factory runs, so read the
        # matched skill + its description here and thread it into the native loop
        # as a first-invocation hint. No match → no hint (loop unchanged). The
        # description is resolved from the SAME per-workspace skill loader the
        # frame matched against.
        suggested_skill, suggested_skill_description = _frame_skill_hint(run, _skill_loader_for)
        # Executor-pool Lift 5b: a ``provider='executor'`` account routes to a
        # registered external CLI worker, NOT the native LLM loop. Dispatch a
        # task + await the worker's result (ExecutorOrchestrator); the api-llm
        # path below is unchanged. The redis client is threaded in by
        # ``run_workers`` (built whenever a Redis URL is configured); a None
        # client → the orchestrator raises a Decision (cannot dispatch).
        if is_executor_account(account):
            # B2b — executor verification convergence. The orchestrator now runs
            # the SAME verification the native loop runs, so it needs the same
            # sandbox manager (mount the run dir to run command checks) and a
            # judge LLM. ``box`` is the real (or Noop) sandbox already built for
            # the deps. ``verify_llm`` resolves a NON-executor active account for
            # the judge (mirrors the settle-extractor resolution) — None when
            # only executor accounts are active (→ judge-bearing contracts route
            # to human review). ``retriever`` folds in the workspace's BSage
            # canon (B3 / RC-2 fix — was always None before).
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
        # B9b — knowledge-only short-circuit (the cost saver). B9a recorded
        # ``run.payload["frame"]["path_classification"]`` but only the agent loop
        # ran. When the frame classified the ask as ``knowledge_only`` (a question
        # answerable from the workspace's BSage ontology), route to the
        # KnowledgeAnswerOrchestrator: ONE LLM call composes an answer grounded in
        # retrieved knowledge — NO plan→act→verify loop, no sandbox, no verify, no
        # PROVED. The work LLM is the same gateway-resolved api-llm above (an
        # executor account already returned above — knowledge-only is for the LLM
        # Q&A path, never a delegated CLI worker). Any other classification (the
        # ``agent_loop`` default + the B9a keyword fallback) falls through to the
        # native loop below — unchanged.
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
        # B5a — thread the run's workspace SkillLoader into the native loop so it
        # registers the ``invoke_skill`` + ``knowledge_search`` tools. The loader
        # is the SAME per-workspace one the FrameStage uses (``<skills_root>/
        # <workspace_id>/``, already ``load_all()``-ed). Before B5a the loop's
        # tool set was a static 6-tuple — skills were authored but the loop could
        # never call them, and it could not consult knowledge on demand.
        skill_loader = _skill_loader_for(run.workspace_id)
        # B5b — connector-action provider. When the worker loaded the plugin
        # registry (``run_workers`` does), the native loop can take the
        # workspace's ``mcp_exposed`` connector actions mid-run. None (no
        # plugins loaded — every legacy caller/test) → no connector tools,
        # loop unchanged. Built per-run with the run's session (mirrors the
        # orchestrator itself). Lift 0c removed the load-time danger gating.
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
    # when run.product_id is set), else leave the empty scratch dir (the
    # Direct-path tests + no-product runs are unaffected).
    #
    # The cipher is resolved LAZILY inside the github branch only — non-
    # github runs never force the KMS key.
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
        # B9a — the cheap-LLM framing seam, resolved per-workspace via the gateway
        # (mirrors the settle extractor). FrameStage uses it for real framing;
        # None (zero/ambiguous/executor-only account) → keyword fallback.
        frame_llm=_frame_llm_for,
    )


# ---------------------------------------------------------------------------
# Settle entity-extractor factory — concepts from LLM-extracted entities
# ---------------------------------------------------------------------------
#
# The settle→knowledge path derives concepts from EXTRACTED ENTITIES (BSage's
# mechanism) instead of by tokenizing the work summary. This factory builds a
# per-workspace IngestCompiler whose CompileLlm seam routes the extraction call
# through the SAME GatewayDispatcher the chat/agent paths use. Resolution is the
# "exactly one active account → use it" policy; ZERO/MANY (or no LLM) returns
# None so the sink soft-falls back to the deterministic heuristic — extraction
# is derived knowledge, not a run, so it never raises a founder Decision.


class _GatewayCompileLlm:
    """Adapts :class:`GatewayDispatcher` to the ``CompileLlm`` seam.

    Maps a single ``chat(system, messages, ...)`` call to a ``DispatchRequest``
    and returns the response content string. The account/model identity is
    resolved once (per workspace) by the factory and held for the call. Mirrors
    :class:`~backend.workflow.application.loop_llm.GatewayLoopLlm`, but for the plain
    chat-completion (no tools) extraction call."""

    # Substantial-tier features — extraction is a structured-output task that
    # benefits from the heavier model, same as the agent loop's plan/act turns.
    _FEATURES = ClassificationFeatures(
        token_count=4096,
        system_prompt_chars=2048,
        conversation_turns=1,
        code_block_count=0,
        tool_count=0,
    )

    def __init__(
        self,
        *,
        dispatcher: GatewayDispatcher,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> None:
        self._dispatcher = dispatcher
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._model_account_id = model_account_id

    async def chat(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        suppress_reasoning: bool = False,
        timeout_s: float | None = None,
    ) -> str:
        # CompileLlm passes only user messages; the system prompt is a separate
        # arg — prepend it so the dispatcher (OpenAI-style messages) sees it.
        full_messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        full_messages.extend(dict(m) for m in messages)
        request = DispatchRequest(
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            model_account_id=self._model_account_id,
            messages=full_messages,
            features=self._FEATURES,
            projected_cost_cents=1,
        )
        result = await self._dispatcher.dispatch(request)
        return result.response.content


class _GatewayFrameLlm:
    """Adapts :class:`GatewayDispatcher` to the :class:`FrameLlm` seam (B9a).

    The frame stage is a single cheap completion: ``complete_text(system, user)``
    maps to one :class:`DispatchRequest`. Framing is a small classification call,
    so it uses LIGHTER features than the work loop (it benefits from the cheap
    tier — Workflow §1.2 "✓ cheap"). The account/model identity is resolved once
    (per workspace) by the factory and held for the call."""

    # Cheap-tier features — framing is a short interpret/classify call, not the
    # heavy structured-output of the work loop, so it deliberately routes cheaper.
    _FEATURES = ClassificationFeatures(
        token_count=512,
        system_prompt_chars=1024,
        conversation_turns=1,
        code_block_count=0,
        tool_count=0,
    )

    def __init__(
        self,
        *,
        dispatcher: GatewayDispatcher,
        workspace_id: uuid.UUID,
        account_id: uuid.UUID,
        model_account_id: uuid.UUID,
    ) -> None:
        self._dispatcher = dispatcher
        self._workspace_id = workspace_id
        self._account_id = account_id
        self._model_account_id = model_account_id

    async def complete_text(self, *, system: str, user: str) -> str:
        request = DispatchRequest(
            workspace_id=self._workspace_id,
            account_id=self._account_id,
            model_account_id=self._model_account_id,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            features=self._FEATURES,
            projected_cost_cents=1,
        )
        result = await self._dispatcher.dispatch(request)
        return result.response.content


def _is_knowledge_only(run: ExecutionRun) -> bool:
    """Read the frame's ``path_classification`` off ``run.payload`` (B9b).

    The :class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker` records the full frame
    onto ``run.payload["frame"]`` BEFORE the orchestrator factory runs, so the
    knowledge-only branch is available here. ``True`` only when the frame
    explicitly classified the ask ``knowledge_only`` — any other value, a missing
    frame, or a malformed payload is the agent-loop default (no strand)."""
    payload = run.payload or {}
    frame = payload.get("frame") if isinstance(payload, dict) else None
    classification = frame.get("path_classification") if isinstance(frame, dict) else None
    return classification == "knowledge_only"


def _frame_skill_hint(
    run: ExecutionRun, skill_loader_for: Callable[[uuid.UUID], SkillLoader]
) -> tuple[str | None, str | None]:
    """Read the frame's matched skill off ``run.payload`` + resolve its description.

    The :class:`~backend.workflow.infrastructure.workers.agent_worker.AgentWorker` records the frame onto
    ``run.payload["frame"]`` BEFORE the orchestrator factory runs, so the matched
    skill is available here to thread into the loop as a first-invocation hint
    (B9a — the frame output is finally consumed). The description is looked up in
    the SAME per-workspace skill loader the frame matched against. No frame / no
    match / a stale name not in the loader → ``(None, None)`` (no hint)."""
    payload = run.payload or {}
    frame = payload.get("frame") if isinstance(payload, dict) else None
    skill_match = frame.get("skill_match") if isinstance(frame, dict) else None
    if not isinstance(skill_match, str) or not skill_match:
        return None, None
    loader = skill_loader_for(run.workspace_id)
    meta = loader.registry.get(skill_match)
    description = meta.description if meta is not None else None
    return skill_match, description


def build_settle_entity_extractor_factory(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> ExtractorFactory:
    """Production :class:`ExtractorFactory` for the settle sink.

    Per call (one per settlement), resolves the workspace's single active
    ModelAccount and builds an :class:`~backend.knowledge.ingest.ingest_compiler.IngestCompiler`
    rooted at the SAME ``<vault_root>/<region>/<workspace_id>/`` boundary the
    sink writes to, with a :class:`_GatewayCompileLlm` seam over a per-session
    :class:`GatewayDispatcher`. Returns ``None`` (soft-fallback) when the
    workspace has zero or more-than-one active account — never guessing a model
    for derived knowledge."""
    settings = settings or get_settings()
    vault_root = Path(settings.knowledge_vault_root)

    async def _factory(*, region: str, workspace_id: uuid.UUID) -> EntityExtractor | None:
        from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415 — lazy heavy import
        from backend.knowledge.ingest.ingest_compiler import IngestCompiler  # noqa: PLC0415

        async with session_factory() as session:
            accounts = await _list_active_workspace_accounts(session, workspace_id)
            account = _single_native_account(accounts)
            if account is None:
                logger.info(
                    "settle_extractor_account_unresolved",
                    workspace_id=str(workspace_id),
                    active_count=len(accounts),
                )
                return None
            dispatcher = build_gateway_dispatcher(session, settings)
            llm = _GatewayCompileLlm(
                dispatcher=dispatcher,
                workspace_id=workspace_id,
                account_id=account.account_id,
                model_account_id=account.id,
            )
            knowledge = KnowledgeFactory(
                region=region,
                workspace_id=str(workspace_id),
                vault_root=vault_root,
            )
            # retriever omitted (None) — entity extraction needs no vault context;
            # the compiler only extracts names from the seed text, never writes.
            return IngestCompiler(garden_writer=knowledge.writer(), llm_client=llm)

    return _factory


def build_note_embed_hook(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> NoteEmbedHook:
    """Production :class:`NoteEmbedHook` for the settle sink (G5b + G6).

    Per absorbed note, embeds the note summary with the DEPLOYMENT knowledge
    embedder (``settings.knowledge_embedding_model`` — G6, not per-account) and
    upserts the vector into ``note_embeddings`` (pgvector) keyed by the note's
    vault-relative path, so :class:`SemanticNoteRetriever` can find it — the
    pgvector index is the DERIVED index of the Markdown SoT (proposal §5.4).
    Opens its OWN session + commit (decoupled from the settle transaction).
    No-op when no knowledge embedding model is configured — the index simply
    isn't built rather than erroring."""
    settings = settings or get_settings()
    vault_root = Path(settings.knowledge_vault_root)

    async def _hook(settlement: Settlement, node_ref: str) -> None:
        from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
            resolve_knowledge_embedder,
        )
        from backend.knowledge.retrieval.storage.pg import (  # noqa: PLC0415
            PgNoteVectorBackend,
        )

        text = settlement.summary.strip()
        if not text:
            return
        embedder = resolve_knowledge_embedder(settings)
        if not embedder.enabled or embedder.model is None:
            return
        vector = await embedder.embed(text)
        if not vector:
            return
        note_path = _relative_note_path(
            node_ref, vault_root, settlement.region, settlement.workspace_id
        )
        async with session_factory() as session:
            backend = PgNoteVectorBackend(
                session,
                workspace_id=settlement.workspace_id,
                embedding_model=embedder.model,
            )
            await backend.store(note_path, vector)
            await session.commit()

    return _hook


def _relative_note_path(
    node_ref: str, vault_root: Path, region: str, workspace_id: uuid.UUID
) -> str:
    """The note's vault-relative path (e.g. ``garden/seedling/x.md``) for the
    embedding key, so it matches how the other retrievers reference notes. Falls
    back to the raw ``node_ref`` when it isn't under the workspace root (defensive
    — never raises)."""
    workspace_root = vault_root / region / str(workspace_id)
    try:
        return Path(node_ref).relative_to(workspace_root).as_posix()
    except ValueError:
        return node_ref


# ---------------------------------------------------------------------------
# Real delivery dispatch adapter
# ---------------------------------------------------------------------------


class RealPluginDispatchAdapter:
    """Bridges the worker's ``PluginDispatchAdapter`` Protocol to the real
    :class:`DeliveryDispatcher` over the loaded plugins.

    The worker calls ``dispatch(workspace_id, deliverable_id, artifact_type)``;
    this adapter supplies the loaded plugin list. The dispatcher itself filters
    by ``artifact_type`` (a plugin without a matching outbound skips silently),
    so a workspace with no matching plugin yields an empty (but successful)
    :class:`DeliveryResult` — the event still drains, never wedging the queue.
    """

    def __init__(
        self,
        *,
        plugins: list[PluginMeta],
        dispatcher: DeliveryDispatcher | None = None,
    ) -> None:
        self._plugins = plugins
        self._dispatcher = dispatcher or DeliveryDispatcher(runner=PluginRunner())

    async def dispatch(
        self,
        *,
        workspace_id: uuid.UUID,
        deliverable_id: uuid.UUID,
        artifact_type: str,
        plugins: Any = (),
        context: Any = None,
        event: Any = None,
    ) -> DeliveryResult:
        return await self._dispatcher.dispatch(
            workspace_id=workspace_id,
            deliverable_id=deliverable_id,
            artifact_type=artifact_type,  # type: ignore[arg-type]  # validated downstream
            plugins=self._plugins,
            context=context,
            event=event,
        )


async def build_delivery_adapter(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    plugins_dir: Path | None = None,
) -> ConnectorDeliveryAdapter:
    """Load every plugin under ``plugins_dir`` + wrap a connector-bound adapter.

    The production delivery path resolves the run workspace's configured
    ``connector_accounts`` (binding = ``delivery_config``), shapes the connector
    outbound event from the Deliverable content + that stable config, and
    dispatches THAT connector's ``@p.outbound`` — closing the verified-Deliverable
    → external-delivery loop. v1 ships the notion event mapper; other connectors
    are a registered seam (see :mod:`backend.workflow.application.delivery.connector_dispatch`).

    The adapter decrypts the per-account outbound credential
    (``signing_secret_ciphertext``) with the settings-derived
    :class:`CredentialCipher` and opens its own session per dispatch (it must
    load the Deliverable + resolve the binding), so it carries a session
    factory rather than borrowing the worker's row-scoped session.
    """
    root = plugins_dir or _PLUGINS_IMPLEMENTATIONS_DIR
    loader = PluginLoader(root)
    registry = await loader.load_all()
    logger.info("worker_runtime_plugins_loaded", count=len(registry), names=sorted(registry))
    # workspace_root lets the github special case find the run's checkout
    # (``run_workspace_root/<run_id>``) to commit + push before opening the PR.
    return build_connector_delivery_adapter(
        session_factory=session_factory,
        plugins=list(registry.values()),
        cipher=CredentialCipher(_key_from_settings()),
        workspace_root=Path(get_settings().run_workspace_root),
    )


async def load_connector_plugins(
    *,
    settings: Settings | None = None,
    plugins_dir: Path | None = None,
) -> dict[str, PluginMeta]:
    """Load the plugin registry (B5b).

    Returns ``plugins_by_name`` so the native agent loop can surface the
    workspace's ``mcp_exposed`` connector actions as tools. Lift 0c removed
    the load-time danger scanner — the returned registry no longer carries a
    verdict map."""
    settings = settings or get_settings()
    root = plugins_dir or _PLUGINS_IMPLEMENTATIONS_DIR
    loader = PluginLoader(root)
    registry = await loader.load_all()
    logger.info(
        "connector_action_plugins_loaded",
        count=len(registry),
    )
    return dict(registry)


# ---------------------------------------------------------------------------
# Relay sink — drains the audit outbox (no remote connector in this chunk)
# ---------------------------------------------------------------------------


class LoggingRelay:
    """A :class:`~backend.workflow.infrastructure.workers.relay_worker.Relay` that acknowledges every
    record after logging it.

    The remote audit sink (HTTP/gRPC connector) is out of scope for the
    worker-runtime chunk; this relay drains the outbox so audit rows do not
    accumulate unboundedly, by acking the whole batch (every id delivered).
    """

    async def send(self, records: Any) -> list[int]:
        ids = [r.id for r in records]
        if ids:
            logger.info("worker_runtime_relay_acked", count=len(ids))
        return ids


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class WorkerRuntime:
    """Owns the worker set + its shared engine; runs them until stopped."""

    workers: list[BaseWorker]
    _stop: asyncio.Event

    async def run_forever(self) -> None:
        """Start every worker, then block until :meth:`request_stop` / a signal."""
        for worker in self.workers:
            await worker.start()
        logger.info("worker_runtime_started", workers=[w._name for w in self.workers])
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        """Stop every worker (graceful — drains the in-flight tick first)."""
        for worker in self.workers:
            await worker.stop()
        logger.info("worker_runtime_stopped")


async def check_executor_dispatch_health(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis_url: str,
) -> dict[str, Any]:
    """B14 — operator liveness probe for executor dispatch readiness.

    The :class:`backend.executors.orchestrator.ExecutorOrchestrator` dispatches
    a run to a CLI worker by XADDing onto a Redis Stream. When ``settings.redis_url``
    is empty the orchestrator raises a ``no_executor_dispatch_transport``
    :class:`Decision` at run time — a correct, non-silent failure mode, but one
    that only surfaces AFTER an executor run has been minted. An operator that
    has configured executor workers (one or more active
    ``provider='executor'`` :class:`ModelAccount` rows) with no Redis URL set
    will see every executor run fail this way.

    This helper makes the misconfiguration **loud at startup**: it counts
    active executor accounts across all workspaces and, when the count is
    positive AND ``redis_url`` is empty, emits a structured
    ``executor_dispatch_no_redis`` WARNING that points operators at the
    ``BSVIBE_REDIS_URL`` env var. It NEVER crashes — preserves the existing
    runtime contract; it only adds visibility.

    Returns a dict (``healthy``, ``executor_account_count``,
    ``redis_configured``) so a future CLI ``health`` command / smoke probe can
    surface the same signal without re-grepping logs.
    """
    redis_configured = bool(redis_url)
    async with session_factory() as session:
        result = await session.execute(
            select(func.count())
            .select_from(ModelAccount)
            .where(
                ModelAccount.provider == EXECUTOR_PROVIDER,
                ModelAccount.is_active.is_(True),
            )
        )
    count = int(result.scalar() or 0)
    healthy = redis_configured or count == 0
    if not healthy:
        logger.warning(
            "executor_dispatch_no_redis",
            executor_account_count=count,
            hint=(
                "executor accounts are active but BSVIBE_REDIS_URL is empty — "
                "every executor run will raise a 'no_executor_dispatch_transport' "
                "Decision; set BSVIBE_REDIS_URL (e.g. redis://localhost:6387/0) "
                "to enable worker dispatch"
            ),
        )
    return {
        "healthy": healthy,
        "executor_account_count": count,
        "redis_configured": redis_configured,
    }


def build_worker_runtime(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    execution: AgentExecutionDeps,
    delivery_adapter: PluginDispatchAdapter,
    settings: Settings | None = None,
    redis_client: Any = None,
) -> WorkerRuntime:
    """Construct the full worker set against one shared session factory.

    ``redis_client`` is wired into the producer-side workers (the IntakeWorker
    emits an ``agent`` notification per minted Request) ONLY in
    ``worker_mode="redis_streams"``; ``None`` (the default) keeps the pure
    DB-polling behaviour. Emission is gated + soft-fail inside the worker, so
    passing a client in db_polling mode is also a harmless no-op."""
    settings = settings or get_settings()
    workers: list[BaseWorker] = [
        IntakeWorker(
            session_factory=session_factory,
            redis_client=redis_client,
            settings=settings,
        ),
        AgentWorker(session_factory=session_factory, execution=execution),
        DeliveryWorker(session_factory=session_factory, dispatcher=delivery_adapter),
        SettleWorker(
            session_factory=session_factory,
            sink=KnowledgeSettleSink(
                vault_root=Path(settings.knowledge_vault_root),
                # PRIMARY: derive concepts from LLM-extracted entities (BSage's
                # mechanism) — soft-falls back to the deterministic heuristic when
                # the workspace has no single active model account.
                extractor_factory=build_settle_entity_extractor_factory(
                    session_factory=session_factory, settings=settings
                ),
            ),
            config=SettleWorkerConfig(default_region=settings.knowledge_default_region),
            # Close the §5 ratchet loop: after each drain batch, promote each
            # affected workspace's garden observations into canon over the SAME
            # vault boundary the sink wrote to.
            promoter_factory=build_garden_promoter_factory(
                vault_root=Path(settings.knowledge_vault_root)
            ),
            # G5b — populate the pgvector note store from each absorbed note so
            # G5a's SemanticNoteRetriever has data to search. No-op until a
            # workspace configures an embedding model.
            embed_hook=build_note_embed_hook(session_factory=session_factory, settings=settings),
        ),
        # Config-driven relay: HttpRelay when ``audit_relay_url`` is set,
        # else the no-sink LoggingRelay default (drain + ack, no delivery).
        RelayWorker(session_factory=session_factory, relay=build_relay(settings)),
        # M1 — schedule runner. DB-polls ``workspace_schedules`` for rows where
        # ``enabled=True AND next_run_at <= now`` and fires
        # :class:`ScheduleTrigger` on each (downstream IntakeWorker then drains
        # the new TriggerEvent into a Request — exactly the same path a Direct
        # or Connector trigger walks). Depends on the
        # :class:`ScheduleRunnerProtocol` so the Redis-Streams wake-up
        # substrate (Status §5 Phase-1 honest defer) can be dropped in later
        # without touching the worker. Until then the DB-poll runner is the
        # real mode; cron-algebra defaults to one-shot (see
        # :class:`OneShotScheduleAdvancer`) — operators wire a recurring
        # advancer via the factory.
        ScheduleWorker(
            session_factory=session_factory,
            runner=build_db_poll_schedule_runner(),
        ),
        # D3a — Safe Mode expiry sweep. A SECOND ScheduleWorker against the
        # SAME ScheduleRunnerProtocol seam but a different runner: the
        # :class:`SafeModeExpirySweepRunner` selects every PENDING/EXTENDED
        # safe_mode_queue_items row past ``expires_at`` (across ALL
        # workspaces), transitions each via
        # :meth:`SafeModeQueue.mark_expired`, and emits ONE
        # ``safe_mode.expired`` AuditOutboxRecord per non-empty batch
        # tagged ``trigger=schedule, source=system.safe_mode_expiry`` —
        # the glass-box provenance D3b will subscribe to on the
        # compensation side. ``workspace_schedules`` is intentionally NOT
        # used here (it requires ``workspace_id NOT NULL`` — a system-wide
        # sweep doesn't fit that invariant); the Protocol seam is what M1
        # shipped, and one extra worker instance on the same seam is the
        # honest reuse. A distinct ``name`` keeps the two workers'
        # task-name + log-prefix separable.
        ScheduleWorker(
            session_factory=session_factory,
            runner=SafeModeExpirySweepRunner(),
            name="safe_mode_expiry_worker",
            # Hourly is fine — TTLs are day-grained (90d initial + 30d
            # extensions), and a row drifting one tick past ``expires_at``
            # before the sweep catches it has no founder impact. Cheap
            # poll, low write amplification.
            config=ScheduleWorkerConfig(poll_interval_s=3600.0),
        ),
    ]
    return WorkerRuntime(workers=workers, _stop=asyncio.Event())


# ---------------------------------------------------------------------------
# Redis Streams consumer wiring (opt-in — worker_mode="redis_streams")
# ---------------------------------------------------------------------------
#
# This path is purely ADDITIVE. The DB-polling default above is UNTOUCHED. When
# ``worker_mode="redis_streams"`` the daemon drives each worker by a Redis
# Streams consumer (XREADGROUP → handler → XACK) INSTEAD of the poll loop — but
# the handler is the worker's OWN single-tick method (``drain_once`` /
# ``claim_once`` + ``drive_once`` via ``_tick`` / ``drain_once``), so no business
# logic is duplicated: Redis is only a different *trigger* for the same tick.


@dataclass(slots=True)
class StreamConsumerBinding:
    """One worker bound to its source stream + consumer group + tick handler."""

    stream_name: str
    consumer_group: str
    handler: StreamHandler


def _tick_handler(tick: Callable[[], Awaitable[int]]) -> StreamHandler:
    """Adapt a worker's no-arg single-tick method to a stream handler.

    The notification fields are intentionally ignored — the worker's tick reads
    its own source table (the DB row is the source of truth); the stream entry
    is only a wake-up. This keeps the Redis path a pure trigger over the SAME
    DB-driven logic, so a notification for an already-drained row is a harmless
    no-op (the tick simply finds nothing) and a missed notification is still
    caught by any DB-polling deployment."""

    async def _handle(_fields: dict[str, Any]) -> None:
        await tick()

    return _handle


def build_stream_consumers(workers: list[Any]) -> list[StreamConsumerBinding]:
    """Map known workers to their (stream, group, handler) bindings.

    The handler reuses each worker's existing single-tick method:

    * intake_worker → ``intake`` stream, handler = ``drain_once``
    * agent_worker → ``agent`` stream, handler = ``_tick`` (claim + drive)
    * delivery_worker → ``deliver`` stream, handler = ``drain_once``
    * settle_worker → ``settle`` stream, handler = ``drain_once``

    The relay_worker is intentionally OMITTED — it drains the audit outbox on
    its own cadence, not in response to a producer event, so it has no stream.
    A worker whose name is not in the mapping is skipped (not crashed)."""
    stream_by_name: dict[str, str] = {
        "intake_worker": STREAM_INTAKE,
        "agent_worker": STREAM_AGENT,
        "delivery_worker": STREAM_DELIVER,
        "settle_worker": STREAM_SETTLE,
    }
    bindings: list[StreamConsumerBinding] = []
    for worker in workers:
        name = getattr(worker, "_name", None)
        if not isinstance(name, str):
            continue
        stream = stream_by_name.get(name)
        if stream is None:
            continue
        # agent_worker advances through claim + drive in one tick (``_tick``);
        # the queue-style workers expose a single ``drain_once``. Both reach the
        # SAME logic — ``_tick`` simply calls ``drain_once`` (or claim+drive) — so
        # preferring ``_tick`` keeps the trigger faithful to the poll-loop body.
        tick = getattr(worker, "_tick", None)
        if tick is None:
            tick = worker.drain_once
        bindings.append(
            StreamConsumerBinding(
                stream_name=stream,
                consumer_group=name,
                handler=_tick_handler(tick),
            )
        )
    return bindings


async def run_stream_consumers(
    *,
    workers: list[BaseWorker],
    redis_client: Any,
    stop_event: asyncio.Event,
    consumer_name: str = "worker-1",
) -> None:
    """Run a :class:`RedisStreamConsumer` per worker binding until stopped.

    Each consumer loops XREADGROUP → the worker's own tick handler → XACK. The
    relay worker (no stream binding) keeps running on its DB-poll loop so the
    audit outbox still drains; it is started/stopped alongside the consumers."""
    consumer = RedisStreamConsumer(redis_client)
    bindings = build_stream_consumers(list(workers))
    bound_groups = {b.consumer_group for b in bindings}

    # Workers without a stream binding (relay) still poll their own source.
    poll_workers = [w for w in workers if getattr(w, "_name", None) not in bound_groups]
    for w in poll_workers:
        await w.start()

    tasks = [
        asyncio.create_task(
            consumer.consume(
                stream_name=b.stream_name,
                consumer_group=b.consumer_group,
                consumer_name=consumer_name,
                handler=b.handler,
                stop_event=stop_event,
            ),
            name=f"stream::{b.consumer_group}",
        )
        for b in bindings
    ]
    logger.info("worker_runtime_started_redis_streams", streams=sorted(bound_groups))
    try:
        await stop_event.wait()
    finally:
        for w in poll_workers:
            await w.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:  # pragma: no cover — expected on shutdown
                pass


async def run_workers() -> None:
    """Process entrypoint — construct + run every worker until SIGINT/SIGTERM.

    Wired by ``python -m backend.workers`` (see ``backend/workers/__main__.py``).
    Default ``worker_mode="db_polling"`` runs the poll-loop runtime exactly as
    before; ``worker_mode="redis_streams"`` runs the Redis-consumer runtime.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # The Redis client is needed by (a) redis_streams mode's producer-side
    # wake-up emission and (b) executor-pool dispatch (Lift 5b) — the
    # ExecutorOrchestrator XADDs a task onto the worker's stream + awaits the
    # done channel, even in the default db_polling mode. So it is built whenever
    # a Redis URL is configured (the default is set), and threaded through the
    # orchestrator factory. ``decode_responses=True`` matches the dispatch
    # substrate's flat-string contract.
    redis_client: Any = None
    if settings.redis_url:
        redis_client = redis_aio.from_url(settings.redis_url, decode_responses=True)
        # C2 — bind the worker process's LiveEventBus singleton against the
        # SAME Redis transport the backend HTTP container binds against, so
        # audit-emit publishes from the worker (where decision.pending /
        # run.terminal / delivery.queued actually fire) land on the channel
        # the SSE subscribers in the backend container are subscribed to.
        # No Redis URL → in-memory fallback (in-process subscribers — none
        # in the worker — only; SSE in the HTTP container won't see it,
        # which is the regression C2 is here to fix).
        #
        # Skip under pytest for the same reason as create_app() — a real
        # redis client held in the process-wide singleton leaks
        # connection-pool Futures across per-test event loops.
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            # Lazy import to avoid circular module-init: backend.api.v1
            # re-exports safemode which imports backend.workflow.infrastructure.workers.run.
            from backend.api.v1.live_events import (  # noqa: PLC0415
                set_live_event_bus_redis,
            )

            set_live_event_bus_redis(redis_client)
            logger.info("worker_live_event_bus_redis_bound", redis_url=settings.redis_url)

    # B14 — operator visibility: warn LOUDLY at startup when the executor pool
    # is configured but Redis is not. The runtime keeps booting (the warning is
    # informational, not fatal) but the operator now sees a clear pointer at
    # the env var instead of debugging silent run-time decisions later.
    await check_executor_dispatch_health(
        session_factory=session_factory, redis_url=settings.redis_url
    )

    # B5b — load the plugin registry ONCE so each per-run native loop can
    # surface the workspace's connector actions as tools. Shared across runs;
    # the per-run resolver only adds the run's session. Lift 0c removed the
    # load-time danger verdict map.
    connector_plugins = await load_connector_plugins(settings=settings)
    execution = build_agent_execution_deps(
        settings=settings,
        redis_client=redis_client,
        connector_plugins=connector_plugins,
    )
    delivery_adapter = await build_delivery_adapter(session_factory=session_factory)
    runtime = build_worker_runtime(
        session_factory=session_factory,
        execution=execution,
        delivery_adapter=delivery_adapter,
        settings=settings,
        redis_client=redis_client,
    )

    if settings.worker_mode == "redis_streams":
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:  # pragma: no cover — non-POSIX
                pass
        try:
            await run_stream_consumers(
                workers=runtime.workers,
                redis_client=redis_client,
                stop_event=stop_event,
            )
        finally:
            await redis_client.aclose()
            await engine.dispose()
        return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.request_stop)
        except NotImplementedError:  # pragma: no cover — non-POSIX
            pass

    try:
        await runtime.run_forever()
    finally:
        if redis_client is not None:
            await redis_client.aclose()
        await engine.dispose()


# Re-export so ``AuditOutboxRecord`` typing stays importable for any relay
# wiring that grows here later.
__all__ = [
    "AuditOutboxRecord",
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
    "LoggingRelay",
    "RealPluginDispatchAdapter",
    "StreamConsumerBinding",
    "WorkerRuntime",
    "build_agent_execution_deps",
    "build_delivery_adapter",
    "build_gateway_dispatcher",
    "build_settle_entity_extractor_factory",
    "build_stream_consumers",
    "build_worker_runtime",
    "check_executor_dispatch_health",
    "load_connector_plugins",
    "resolve_workspace_model_account",
    "run_stream_consumers",
    "run_workers",
]
