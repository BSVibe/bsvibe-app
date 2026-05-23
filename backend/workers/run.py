"""Production worker runtime — the daemon that actually *runs* the workers.

Workflow §11.1 / §12.5 #8 (Bundle G). Phase 1 wired the Direct path end to
end and proved it with ``tests/glue/test_direct_path_e2e.py`` (single-tick
methods, in a test). But nothing in production *runs* the DB-polling workers
or injects real execution dependencies — so a real founder POST landed a
TriggerEvent and then nothing drove it.

This module stands up the production runtime:

* :func:`build_agent_execution_deps` — the real
  :class:`~backend.workers.agent_worker.AgentExecutionDeps`: the gateway
  work-LLM (built the same way ``backend.api.v1.chat`` builds its
  dispatcher), the real (or Noop) sandbox manager, the workspace skill
  loader, and a per-run orchestrator factory that resolves the run's
  workspace ModelAccount.
* :func:`resolve_workspace_model_account` — the Phase 2 v1 resolution
  policy (exactly one active account → use it; zero / many → create a
  :class:`~backend.execution.db.Decision`, leave the run RUNNING — never a
  silent guess or stall).
* :class:`RealPluginDispatchAdapter` — bridges the worker's
  :class:`~backend.workers.delivery_worker.PluginDispatchAdapter` Protocol to
  the real :class:`~backend.delivery.dispatcher.DeliveryDispatcher` over the
  plugins discovered by :class:`~backend.plugins.loader.PluginLoader`.
* :class:`WorkerRuntime` / :func:`run_workers` — construct + concurrently run
  every worker with a shared session factory and graceful SIGINT/SIGTERM
  shutdown (reusing each worker's :meth:`BaseWorker.start` / ``stop`` —
  the poll loop is not reinvented here).

DB-polling, not Redis Streams (Phase 1 invariant retained).
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.accounts.crypto import CredentialCipher, _key_from_settings
from backend.accounts.models import ModelAccount
from backend.accounts.service import ModelAccountService
from backend.config import Settings, get_settings
from backend.delivery.dispatcher import DeliveryDispatcher
from backend.delivery.schema import DeliveryResult
from backend.execution.db import Decision, ExecutionRun
from backend.execution.loop_llm import GatewayLoopLlm
from backend.execution.orchestrator import RunOrchestrator
from backend.gateway.budget.policy import BudgetPolicyService
from backend.gateway.budget.repository import BudgetPolicyRepository
from backend.gateway.budget.tracker import BudgetTracker, InMemoryBudgetStore
from backend.gateway.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.gateway.classifier.static import StaticClassifier
from backend.gateway.dispatch import GatewayDispatcher
from backend.gateway.llm_client import LlmClient
from backend.plugins.base import PluginMeta
from backend.plugins.loader import PluginLoader
from backend.plugins.runner import PluginRunner
from backend.skills.loader import SkillLoader
from backend.supervisor.audit.models import AuditOutboxRecord
from backend.supervisor.sandbox import (
    NoopSandboxManager,
    SandboxManager,
    build_sandbox_manager,
)
from backend.workers.agent_worker import AgentExecutionDeps, AgentWorker
from backend.workers.base import BaseWorker
from backend.workers.delivery_worker import DeliveryWorker
from backend.workers.intake_worker import IntakeWorker
from backend.workers.relay_worker import RelayWorker
from backend.workers.settle_worker import (
    KnowledgeSettleSink,
    SettleWorker,
    SettleWorkerConfig,
    build_garden_promoter_factory,
)

logger = structlog.get_logger(__name__)

# Default plugin-implementations directory (scanned at module import, in sync
# context, so the async loader path stays free of filesystem-resolve calls).
_PLUGINS_IMPLEMENTATIONS_DIR = (
    Path(__file__).resolve().parent.parent / "plugins" / "implementations"
)


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


async def resolve_workspace_model_account(
    session: AsyncSession, run: ExecutionRun
) -> ModelAccount | None:
    """Resolve the workspace's *active* ModelAccount for this run.

    Phase 2 v1 policy (implemented EXACTLY):

    * exactly one active account → return it.
    * ZERO or MORE-THAN-ONE → do NOT crash, do NOT silently guess: create a
      :class:`~backend.execution.db.Decision` (so the run is paused on a
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


# ---------------------------------------------------------------------------
# Production AgentExecutionDeps
# ---------------------------------------------------------------------------


def build_agent_execution_deps(
    *,
    settings: Settings | None = None,
    sandbox_manager: SandboxManager | None = None,
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
    """
    settings = settings or get_settings()
    box: SandboxManager = sandbox_manager or build_sandbox_manager() or NoopSandboxManager()
    skills_root = Path(settings.skills_root)

    def _skill_loader_for(workspace_id: uuid.UUID) -> SkillLoader:
        loader = SkillLoader(skills_root / str(workspace_id))
        loader.load_all()
        return loader

    async def _factory(session: AsyncSession, run: ExecutionRun) -> RunOrchestrator | None:
        account = await resolve_workspace_model_account(session, run)
        if account is None:
            return None
        dispatcher = build_gateway_dispatcher(session, settings)
        llm = GatewayLoopLlm(
            dispatcher=dispatcher,
            workspace_id=run.workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )
        return RunOrchestrator(session=session, llm=llm, sandbox_manager=box)

    return AgentExecutionDeps(
        skill_loader_for=_skill_loader_for,
        orchestrator_factory=_factory,
        workspace_root=Path(settings.run_workspace_root),
    )


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


async def build_delivery_adapter(*, plugins_dir: Path | None = None) -> RealPluginDispatchAdapter:
    """Load every plugin under ``plugins_dir`` and wrap a real dispatcher."""
    root = plugins_dir or _PLUGINS_IMPLEMENTATIONS_DIR
    loader = PluginLoader(root)
    registry = await loader.load_all()
    logger.info("worker_runtime_plugins_loaded", count=len(registry), names=sorted(registry))
    return RealPluginDispatchAdapter(plugins=list(registry.values()))


# ---------------------------------------------------------------------------
# Relay sink — drains the audit outbox (no remote connector in this chunk)
# ---------------------------------------------------------------------------


class LoggingRelay:
    """A :class:`~backend.workers.relay_worker.Relay` that acknowledges every
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


def build_worker_runtime(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    execution: AgentExecutionDeps,
    delivery_adapter: RealPluginDispatchAdapter,
    settings: Settings | None = None,
) -> WorkerRuntime:
    """Construct the full worker set against one shared session factory."""
    settings = settings or get_settings()
    workers: list[BaseWorker] = [
        IntakeWorker(session_factory=session_factory),
        AgentWorker(session_factory=session_factory, execution=execution),
        DeliveryWorker(session_factory=session_factory, dispatcher=delivery_adapter),
        SettleWorker(
            session_factory=session_factory,
            sink=KnowledgeSettleSink(vault_root=Path(settings.knowledge_vault_root)),
            config=SettleWorkerConfig(default_region=settings.knowledge_default_region),
            # Close the §5 ratchet loop: after each drain batch, promote each
            # affected workspace's garden observations into canon over the SAME
            # vault boundary the sink wrote to.
            promoter_factory=build_garden_promoter_factory(
                vault_root=Path(settings.knowledge_vault_root)
            ),
        ),
        RelayWorker(session_factory=session_factory, relay=LoggingRelay()),
    ]
    return WorkerRuntime(workers=workers, _stop=asyncio.Event())


async def run_workers() -> None:
    """Process entrypoint — construct + run every worker until SIGINT/SIGTERM.

    Wired by ``python -m backend.workers`` (see ``backend/workers/__main__.py``).
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, future=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    execution = build_agent_execution_deps(settings=settings)
    delivery_adapter = await build_delivery_adapter()
    runtime = build_worker_runtime(
        session_factory=session_factory,
        execution=execution,
        delivery_adapter=delivery_adapter,
        settings=settings,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runtime.request_stop)
        except NotImplementedError:  # pragma: no cover — non-POSIX
            pass

    try:
        await runtime.run_forever()
    finally:
        await engine.dispose()


# Re-export so ``AuditOutboxRecord`` typing stays importable for any relay
# wiring that grows here later.
__all__ = [
    "AuditOutboxRecord",
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
    "LoggingRelay",
    "RealPluginDispatchAdapter",
    "WorkerRuntime",
    "build_agent_execution_deps",
    "build_delivery_adapter",
    "build_gateway_dispatcher",
    "build_worker_runtime",
    "resolve_workspace_model_account",
    "run_workers",
]
