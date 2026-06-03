"""Product-bootstrap background runtime (Lift A v2).

Wires the :mod:`backend.products.application.bootstrap` orchestrator to the
production fabric:

* :class:`SqlAlchemyBootstrapRepository` — concrete
  :class:`BootstrapRepository` backed by ``products.bootstrap_*`` columns.
* :func:`build_bootstrap_knowledge` — assembles a :class:`Knowledge` facade
  whose ``ingest_callable`` runs the workspace's
  :class:`IngestCompiler.compile_batch` against the artifact list.
* :func:`run_product_bootstrap_job` — the entrypoint a caller (today the
  :func:`POST /api/v1/products` handler, via :func:`schedule_product_bootstrap`)
  invokes to perform the whole clone → walk → ingest dance. Catches
  every failure mode and writes a precise ``bootstrap_status`` row.
* :func:`schedule_product_bootstrap` — fire-and-forget ``asyncio.create_task``
  shim the API handler uses so the create-product response returns 201
  instantly while the bootstrap proceeds in the background. The task
  reference is held in a module-level set so the garbage collector can't
  drop it mid-clone.

The runtime layer is the only one that imports SQLAlchemy + the concrete
:class:`KnowledgeFactory` — keeps the application layer's repository
Protocol seam clean.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.knowledge.facade import (
    CanonRetrievalResult,
    IngestRequest,
    IngestResult,
    Knowledge,
)
from backend.products.application.bootstrap import (
    BootstrapProgress,
    BootstrapRepository,
    BootstrapTooLargeError,
    register_bootstrap_anchors,
    run_repo_bootstrap,
)
from backend.storage.product_workspace import (
    ProductWorkspaceError,
    product_workspace_path,
)
from backend.workflow.application.runtime.account_resolution import (
    _list_active_workspace_accounts,
    _single_native_account,
)
from backend.workflow.application.runtime.dispatcher import (
    _GatewayCompileLlm,
    build_gateway_dispatcher,
)
from backend.workflow.infrastructure.delivery.git_ops import GitError, GitOps

logger = structlog.get_logger(__name__)


#: Audit event names. ``audit.product.bootstrap_*`` namespacing matches
#: existing ``audit.<domain>.<action>`` convention (see ``connectors.py``
#: ``_AUDIT_IMPORT_*``). Structured-log emissions only; the audit relay
#: consumer picks them up off the same channel.
_AUDIT_STARTED = "audit.product.bootstrap_started"
_AUDIT_COMPLETED = "audit.product.bootstrap_completed"
_AUDIT_FAILED = "audit.product.bootstrap_failed"


#: Lifecycle vocabulary — kept here so the API surface + the PWA can
#: agree on the exact strings without a free-string drift. Mirrors the
#: migration's docstring.
STATUS_PENDING = "pending"
STATUS_CLONING = "cloning"
STATUS_ANALYZING = "analyzing"
STATUS_INGESTING = "ingesting"
STATUS_COMPLETE = "complete"
STATUS_FAILED_CLONE = "failed:clone"
STATUS_FAILED_TOO_LARGE = "failed:too_large"
STATUS_FAILED_INGEST = "failed:ingest"


# Module-level scratchpad of running tasks. ``asyncio.create_task`` returns
# a Task that the event loop only keeps weakly — without a strong ref the
# GC can collect it mid-run. Holding the references here (and discarding
# on completion) is the canonical fire-and-forget pattern.
_running: set[asyncio.Task[None]] = set()


class SqlAlchemyBootstrapRepository:
    """Concrete :class:`BootstrapRepository` against ``products.bootstrap_*``.

    One instance per call — opens its own session via the factory so every
    status flip commits independently of the bootstrap pipeline's session
    lifetime. Keeps the founder's progress UI fresh as each stage flips.
    """

    __slots__ = ("_session_factory",)

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def mark_status(
        self,
        product_id: uuid.UUID,
        *,
        status: str,
        run_id: uuid.UUID | None = None,
        artifacts_count: int | None = None,
        error: str | None = None,
    ) -> None:
        async with self._session_factory() as session:
            row = await session.get(ProductRow, product_id)
            if row is None:
                logger.warning(
                    "bootstrap_status_target_missing",
                    product_id=str(product_id),
                    status=status,
                )
                return
            row.bootstrap_status = status
            if run_id is not None:
                row.bootstrap_run_id = run_id
            if artifacts_count is not None:
                row.bootstrap_artifacts_count = artifacts_count
            if error is not None:
                # Trim — the column is TEXT but we don't want a 100KB
                # stack trace fragment in the status row.
                row.bootstrap_error = error[:2000]
            await session.commit()

    async def fetch_progress(
        self, product_id: uuid.UUID, *, workspace_id: uuid.UUID
    ) -> BootstrapProgress | None:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ProductRow).where(
                    ProductRow.id == product_id,
                    ProductRow.workspace_id == workspace_id,
                )
            )
            if row is None:
                return None
            return BootstrapProgress(
                product_id=row.id,
                status=row.bootstrap_status,
                artifacts_count=row.bootstrap_artifacts_count,
                error=row.bootstrap_error,
                run_id=row.bootstrap_run_id,
                # v1 uses the row timestamps as the lifecycle markers —
                # ``updated_at`` is bumped on every status flip and the
                # founder UI just shows "starting <relative>". A dedicated
                # ``bootstrap_started_at`` / ``…_completed_at`` is a
                # future lift if the founder ever needs absolute times.
                started_at=row.created_at if row.bootstrap_status else None,
                completed_at=row.updated_at if row.bootstrap_status == STATUS_COMPLETE else None,
            )


def build_bootstrap_knowledge(
    *,
    session: AsyncSession,
    workspace_id: uuid.UUID,
    region: str,
    settings: Settings | None = None,
) -> Knowledge | None:
    """Build the workspace's :class:`Knowledge` facade for the bootstrap path.

    Same single-active-account resolution policy as the settle path
    (``_single_native_account`` → use it; zero / ambiguous → ``None`` so the
    bootstrap pipeline soft-fails with ``failed:ingest`` rather than
    guessing a model). Returns ``None`` when no LLM is wireable — the
    runner translates that into a status row.

    The returned facade hangs an ``ingest_callable`` over
    :class:`IngestCompiler.compile_batch` with a per-session
    :class:`_GatewayCompileLlm` (the same plumbing the settle extractor
    factory uses, so a workspace's LLM swap propagates here automatically).
    The settle / retrieve callables are stubs (the bootstrap doesn't drive
    them) — the facade is intentionally narrow for this caller.
    """
    settings = settings or get_settings()
    return _build_bootstrap_knowledge_inner(
        session=session,
        workspace_id=workspace_id,
        region=region,
        settings=settings,
    )


def _build_bootstrap_knowledge_inner(
    *,
    session: AsyncSession,
    workspace_id: uuid.UUID,
    region: str,
    settings: Settings,
) -> Knowledge | None:
    # Lazy imports keep the runtime layer's top-level cheap and avoid the
    # heavy Knowledge subsystem at module load.
    from backend.knowledge.canonicalization.index import (  # noqa: PLC0415
        InMemoryCanonicalizationIndex,
    )
    from backend.knowledge.canonicalization.lock import AsyncIOMutationLock  # noqa: PLC0415
    from backend.knowledge.canonicalization.resolver import TagResolver  # noqa: PLC0415
    from backend.knowledge.canonicalization.service import (  # noqa: PLC0415
        CanonicalizationService,
    )
    from backend.knowledge.canonicalization.store import NoteStore  # noqa: PLC0415
    from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415
    from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415
    from backend.knowledge.ingest.ingest_compiler import (  # noqa: PLC0415
        BatchItem,
        IngestCompiler,
    )

    async def _build_canonicalization_service(
        ws_id: uuid.UUID, region_str: str
    ) -> CanonicalizationService:
        """Build a vault-scoped service so ingest-time tags auto-create concepts.

        Lift A-fix — passing this service to the IngestCompiler closes the gap
        where ``canonicalize_tags`` was a no-op (no service → tags pass through
        unresolved → no ``concepts/active/<id>.md`` written → empty graph view).
        Default permissive policy (Safe Mode off) lets the resolver auto-apply
        a ``create-concept`` action per new tag at write time.
        """
        vault_root = Path(settings.knowledge_vault_root) / region_str / str(ws_id)
        vault_root.mkdir(parents=True, exist_ok=True)
        storage = FileSystemStorage(vault_root)
        index = InMemoryCanonicalizationIndex()
        await index.initialize(storage)
        return CanonicalizationService(
            store=NoteStore(storage),
            lock=AsyncIOMutationLock(),
            index=index,
            resolver=TagResolver(index=index),
        )

    async def _ingest_callable(
        *,
        workspace_id: uuid.UUID,
        region: str,
        artifacts: list[dict[str, object]],
    ) -> tuple[int, int]:
        accounts = await _list_active_workspace_accounts(session, workspace_id)
        account = _single_native_account(accounts)
        if account is None:
            logger.info(
                "bootstrap_ingest_account_unresolved",
                workspace_id=str(workspace_id),
                active_count=len(accounts),
            )
            return (0, 0)
        dispatcher = build_gateway_dispatcher(session, settings)
        llm = _GatewayCompileLlm(
            dispatcher=dispatcher,
            workspace_id=workspace_id,
            account_id=account.account_id,
            model_account_id=account.id,
        )
        factory = KnowledgeFactory(
            region=region,
            workspace_id=str(workspace_id),
            vault_root=Path(settings.knowledge_vault_root),
        )
        canon_service = await _build_canonicalization_service(workspace_id, region)
        compiler = IngestCompiler(
            garden_writer=factory.writer(),
            llm_client=llm,
            canonicalization_service=canon_service,
        )
        items = [
            BatchItem(label=str(a.get("label", "")), content=str(a.get("content", "")))
            for a in artifacts
        ]
        result = await compiler.compile_batch(items, seed_source="product-bootstrap")
        return (result.notes_created, result.notes_updated)

    async def _settle_stub() -> int:
        return 0

    async def _retrieve_stub(**_: object) -> list[dict[str, object]]:
        return []

    # Local concrete that satisfies the ``Knowledge`` Protocol without
    # importing the SqlAlchemyKnowledge concrete (which would pull in the
    # CanonRetriever construction wiring the bootstrap doesn't need).
    class _BootstrapKnowledge:
        async def ingest(self, request: IngestRequest) -> IngestResult:
            created, updated = await _ingest_callable(
                workspace_id=request.workspace_id,
                region=request.region,
                artifacts=list(request.artifacts),
            )
            return IngestResult(
                proposals_count=0,
                notes_count=created + updated,
                run_id=uuid.uuid5(uuid.NAMESPACE_URL, f"bootstrap:{request.workspace_id}"),
            )

        async def retrieve_canon(self, query: Any) -> CanonRetrievalResult:
            del query
            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int:
            del workspace_id, region
            return await _settle_stub()

    return _BootstrapKnowledge()


async def run_product_bootstrap_job(
    *,
    product_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    repo: BootstrapRepository | None = None,
    git_ops: GitOps | None = None,
) -> None:
    """End-to-end: clone → walk → ingest → mark status.

    Designed to run inside an ``asyncio.create_task`` from the API handler.
    Every failure mode is caught and persisted as a precise
    ``bootstrap_status`` row; the founder UI polls
    ``GET /api/v1/products/{id}/bootstrap`` to see the next state.

    ``repo`` / ``git_ops`` are injected for the test path (in-memory
    fakes); production wires the SQLAlchemy concrete + real subprocess
    GitOps.
    """
    settings = settings or get_settings()
    repo = repo or SqlAlchemyBootstrapRepository(session_factory)
    git_ops = git_ops or GitOps()
    run_id = uuid.uuid4()
    logger.info(
        _AUDIT_STARTED,
        product_id=str(product_id),
        workspace_id=str(workspace_id),
        run_id=str(run_id),
        repo_url=repo_url,
    )

    # Look up the workspace's region (vault paths + knowledge-facade
    # construction need it).
    async with session_factory() as session:
        ws = await session.get(WorkspaceRow, workspace_id)
        if ws is None:
            await repo.mark_status(
                product_id,
                status=STATUS_FAILED_INGEST,
                run_id=run_id,
                error="workspace not found",
            )
            logger.warning(
                _AUDIT_FAILED,
                product_id=str(product_id),
                workspace_id=str(workspace_id),
                reason="workspace_missing",
            )
            return
        region = ws.region

    repo_path = product_workspace_path(product_id)

    # The empty ``init_product_workspace`` already ran in the API handler
    # so the dir + ``.git`` exist. For the clone path we wipe + clone
    # fresh — the marker commit is irrelevant once a real repo lands.
    await repo.mark_status(product_id, status=STATUS_CLONING, run_id=run_id)
    try:
        if repo_path.exists():
            await _remove_dir(repo_path)
        await git_ops.clone(repo_url, repo_path, token=None, depth=1)
    except (GitError, ProductWorkspaceError, OSError) as exc:
        msg = _short_error(exc)
        await repo.mark_status(
            product_id,
            status=STATUS_FAILED_CLONE,
            run_id=run_id,
            error=msg,
        )
        logger.warning(
            _AUDIT_FAILED,
            product_id=str(product_id),
            workspace_id=str(workspace_id),
            reason="clone",
            error=msg,
        )
        return

    await repo.mark_status(product_id, status=STATUS_ANALYZING)

    async with session_factory() as session:
        knowledge = build_bootstrap_knowledge(
            session=session,
            workspace_id=workspace_id,
            region=region,
            settings=settings,
        )
        if knowledge is None:
            await repo.mark_status(
                product_id,
                status=STATUS_FAILED_INGEST,
                run_id=run_id,
                error="no active LLM account",
            )
            logger.warning(
                _AUDIT_FAILED,
                product_id=str(product_id),
                workspace_id=str(workspace_id),
                reason="no_llm",
            )
            return

        try:
            await repo.mark_status(product_id, status=STATUS_INGESTING)
            outcome = await run_repo_bootstrap(
                repo_root=repo_path,
                workspace_id=workspace_id,
                region=region,
                knowledge=knowledge,
            )
            # Lift A-fix — promote LLM-classified seedling tags into
            # ``concepts/active/<id>.md`` canonical anchors so the PWA
            # Knowledge graph view (which reads ``list_active_concepts``)
            # has nodes to render. Failure here is a soft warning: the
            # seedlings + entity stubs are already on disk and the next
            # bootstrap/settle pass (or the founder's manual promotion)
            # will fill the anchors in.
            await _register_anchors_soft(
                workspace_id=workspace_id,
                region=region,
                settings=settings,
            )
        except BootstrapTooLargeError as exc:
            await repo.mark_status(
                product_id,
                status=STATUS_FAILED_TOO_LARGE,
                run_id=run_id,
                error=str(exc),
            )
            logger.warning(
                _AUDIT_FAILED,
                product_id=str(product_id),
                workspace_id=str(workspace_id),
                reason="too_large",
                metric=exc.metric,
                value=exc.value,
                limit=exc.limit,
            )
            return
        except Exception as exc:  # noqa: BLE001
            await repo.mark_status(
                product_id,
                status=STATUS_FAILED_INGEST,
                run_id=run_id,
                error=_short_error(exc),
            )
            logger.warning(
                _AUDIT_FAILED,
                product_id=str(product_id),
                workspace_id=str(workspace_id),
                reason="ingest",
                error=_short_error(exc),
                exc_info=True,
            )
            return

    await repo.mark_status(
        product_id,
        status=STATUS_COMPLETE,
        run_id=run_id,
        artifacts_count=outcome.artifacts_count,
    )
    logger.info(
        _AUDIT_COMPLETED,
        product_id=str(product_id),
        workspace_id=str(workspace_id),
        run_id=str(run_id),
        artifacts_count=outcome.artifacts_count,
        notes_count=outcome.ingest_result.notes_count,
    )


def schedule_product_bootstrap(
    *,
    product_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
) -> asyncio.Task[None]:
    """Fire-and-forget the bootstrap job; return the task for tests.

    Holds a strong reference in :data:`_running` until completion so the
    event loop's weak-ref scheduler can't drop it. Errors inside the job
    are caught by :func:`run_product_bootstrap_job` itself — the task's
    own ``done`` callback only logs unhandled crashes for visibility.
    """
    task = asyncio.create_task(
        run_product_bootstrap_job(
            product_id=product_id,
            workspace_id=workspace_id,
            repo_url=repo_url,
            session_factory=session_factory,
            settings=settings,
        )
    )
    _running.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task[None]) -> None:
    _running.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "bootstrap_task_unhandled_exception",
            error=str(exc),
            exc_info=exc,
        )


async def _register_anchors_soft(
    *,
    workspace_id: uuid.UUID,
    region: str,
    settings: Settings,
) -> None:
    """Run :func:`register_bootstrap_anchors` against the workspace vault.

    Lift A-fix — after ingest writes seedlings + entity stubs the bootstrap
    needs one promotion pass to land ``concepts/active/<id>.md`` canonical
    anchors, otherwise the PWA Knowledge graph view stays empty (it reads
    :meth:`InMemoryCanonicalizationIndex.list_active_concepts` which scans
    ``concepts/active/``). The vault path mirrors
    :class:`KnowledgeFactory`'s per-workspace root so the promoter and the
    graph endpoint address the same notes.

    Any failure is logged and swallowed: the seedlings + entity stubs are
    already on disk and the next bootstrap/settle pass (or a CLI backfill)
    can retrofit anchors later. A canonicalization hiccup must not turn a
    successful ingest into ``failed:ingest``.
    """
    vault_root = Path(settings.knowledge_vault_root) / region / str(workspace_id)
    if not vault_root.exists():
        logger.info(
            "bootstrap_anchor_registration_vault_missing",
            workspace_id=str(workspace_id),
            region=region,
        )
        return

    from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415

    try:
        result = await register_bootstrap_anchors(FileSystemStorage(vault_root))
    except Exception as exc:  # noqa: BLE001 — soft-fail per docstring
        logger.warning(
            "bootstrap_anchor_registration_failed",
            workspace_id=str(workspace_id),
            region=region,
            error=str(exc),
            exc_info=True,
        )
        return
    logger.info(
        "bootstrap_anchor_registration_done",
        workspace_id=str(workspace_id),
        region=region,
        created_concepts=len(result.created_concepts),
        candidate_tags=len(result.candidate_tags),
    )


async def _remove_dir(path: Path) -> None:
    """``shutil.rmtree`` off the event loop, suppressing FileNotFound."""
    import shutil  # noqa: PLC0415 — only on the rare wipe path

    def _do() -> None:
        with suppress(FileNotFoundError):
            shutil.rmtree(path)

    await asyncio.to_thread(_do)


def _short_error(exc: BaseException) -> str:
    """One-line error summary suitable for the ``bootstrap_error`` column."""
    message = str(exc).strip().splitlines()
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message[0]}"[:512]


__all__ = [
    "STATUS_ANALYZING",
    "STATUS_CLONING",
    "STATUS_COMPLETE",
    "STATUS_FAILED_CLONE",
    "STATUS_FAILED_INGEST",
    "STATUS_FAILED_TOO_LARGE",
    "STATUS_INGESTING",
    "STATUS_PENDING",
    "SqlAlchemyBootstrapRepository",
    "build_bootstrap_knowledge",
    "run_product_bootstrap_job",
    "schedule_product_bootstrap",
]
