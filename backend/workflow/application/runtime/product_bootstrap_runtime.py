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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings, get_settings
from backend.dispatch.caller_registry import CALLER_KNOWLEDGE_INGEST
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
from backend.shared.core.http import redact_url_password
from backend.storage.product_workspace import (
    ProductWorkspaceError,
    product_workspace_path,
)
from backend.workflow.application.runtime.account_resolution import _resolve_via_caller
from backend.workflow.application.runtime.dispatcher import _ResolverCompileLlm
from backend.workflow.domain.gate_scaffold import scaffold_gate
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

# Lift E13 — per-product index of the running task, so an operator can
# cancel a wedged bootstrap mid-flight (the qazasa123 dogfood symptom:
# bootstrap stuck "ingesting" for 6+ hours, no way to abort without
# slug-churning a fresh product). Maintained as a ``product_id → Task``
# map alongside :data:`_running`; the ``done`` callback unregisters by
# task identity so re-running the same product_id later doesn't leak.
_running_by_product: dict[uuid.UUID, asyncio.Task[None]] = {}


def register_running_task(product_id: uuid.UUID, task: asyncio.Task[None]) -> None:
    """Register ``task`` as the current bootstrap for ``product_id``.

    Public so test scaffolding (and any future external scheduler) can
    surface its task to the cancel surface without going through
    :func:`schedule_product_bootstrap`. Replaces any prior entry for the
    same product_id — the schedule_retry path always supersedes a
    previously-failed bootstrap's stale entry.
    """
    _running_by_product[product_id] = task


def unregister_running_task(product_id: uuid.UUID) -> None:
    """Drop the registered task for ``product_id`` if any.

    Public so test scaffolding can clean up between cases. The done
    callback already calls this via :func:`_on_task_done` in production.
    """
    _running_by_product.pop(product_id, None)


def get_running_task(product_id: uuid.UUID) -> asyncio.Task[None] | None:
    """Return the registered in-flight bootstrap task for ``product_id``."""
    return _running_by_product.get(product_id)


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


class _BootstrapProgressSubscriber:
    """Lift E9 — turn ``INGEST_COMPILE_BATCH_*`` events into ``bootstrap_progress`` writes.

    The bootstrap pipeline emits per-chunk events via the
    :class:`~backend.knowledge._internal.events.EventBus` wired into the
    :class:`IngestCompiler`. This subscriber listens for the four chunk
    lifecycle events and writes the rolling totals onto the product row's
    new ``bootstrap_progress`` JSON column.

    Each event triggers a SHORT-LIVED session (open → ``UPDATE products
    SET bootstrap_progress=… WHERE id=…`` → commit → close). NEVER holds
    the row across a chunk — multi-tenant write contention would stall
    every other product in the workspace while one slow bootstrap runs.

    Writes are best-effort: a transient DB error logs a warning and the
    compile keeps going. ``chunks_done`` is monotonic so last-writer-wins
    semantics are safe — a lost event just stalls the visible counter
    for one tick; the next chunk's event catches up.
    """

    __slots__ = ("_session_factory", "_product_id", "_state")

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        product_id: uuid.UUID,
    ) -> None:
        self._session_factory = session_factory
        self._product_id = product_id
        self._state: dict[str, Any] = {
            "chunks_done": 0,
            "chunks_total": 0,
            "chunks_failed": 0,
            "notes_created": 0,
            "notes_updated": 0,
            "phase": "ingesting",
        }

    async def on_event(self, event: Any) -> None:  # EventSubscriber Protocol
        """Receive one :class:`~backend.knowledge._internal.events.Event`.

        Filters to the ``INGEST_COMPILE_BATCH_*`` event family and folds
        the payload into the rolling state, then writes the snapshot.
        """
        # Lazy import — keep the runtime module's top-level cheap.
        from backend.knowledge._internal.events import EventType  # noqa: PLC0415

        event_type = getattr(event, "event_type", None)
        payload = getattr(event, "payload", {}) or {}

        if event_type is EventType.INGEST_COMPILE_BATCH_START:
            # Carry the chunk_count immediately so the founder UI can
            # render the denominator while chunk 0 is still warming up.
            self._state["chunks_total"] = int(payload.get("chunk_count") or 0)
        elif event_type is EventType.INGEST_COMPILE_BATCH_CHUNK_START:
            # Idempotent: if START never fired (subscribers can join
            # mid-compile) honour the chunk_count carried on the chunk
            # event so the founder UI never shows ``chunks_total=0``
            # alongside a non-zero ``chunks_done``.
            total = int(payload.get("chunk_count") or 0)
            self._state["chunks_total"] = max(self._state["chunks_total"], total)
        elif event_type is EventType.INGEST_COMPILE_BATCH_CHUNK_DONE:
            self._state["chunks_done"] += 1
            self._state["notes_created"] += int(payload.get("notes_created") or 0)
            self._state["notes_updated"] += int(payload.get("notes_updated") or 0)
            total = int(payload.get("chunk_count") or 0)
            self._state["chunks_total"] = max(self._state["chunks_total"], total)
        elif event_type is EventType.INGEST_COMPILE_BATCH_CHUNK_FAILED:
            self._state["chunks_done"] += 1
            self._state["chunks_failed"] += 1
            total = int(payload.get("chunk_count") or 0)
            self._state["chunks_total"] = max(self._state["chunks_total"], total)
        else:
            # Other event types pass through silently — the subscriber
            # only owns the ``INGEST_COMPILE_BATCH_*`` family.
            return

        await self._write_snapshot()

    async def _write_snapshot(self) -> None:
        """Persist the current rolling state. Short-lived session.

        Failure here is logged but never raised — a transient DB hiccup
        must not sink a successful ingest's chunk_done event.
        """
        try:
            async with self._session_factory() as session:
                row = await session.get(ProductRow, self._product_id)
                if row is None:
                    return
                # Fresh dict copy so SQLAlchemy sees a mutation on JSON
                # column (a mutate-in-place dict on a JSON-typed mapped
                # attr can be missed by the dirty tracker on some
                # dialects — copy is cheap and unambiguous).
                row.bootstrap_progress = dict(self._state)
                await session.commit()
        except Exception:  # noqa: BLE001 — best-effort visibility, never block ingest
            logger.warning(
                "bootstrap_progress_write_failed",
                product_id=str(self._product_id),
                exc_info=True,
            )


@dataclass(frozen=True, slots=True)
class _IngestCallResult:
    """Internal tuple returned by ``_ingest_callable`` — Lift E8 Bug 2.

    Adds the compile-time failure signal alongside the created/updated
    counts so the bootstrap runtime can mark ``failed:ingest`` when
    EVERY chunk dropped (notes_created + notes_updated == 0 AND
    chunk_failures > 0). Without this, a workspace whose executor adapter
    couldn't reach Redis silently flipped to ``complete`` with zero notes
    written.
    """

    notes_created: int
    notes_updated: int
    chunk_failures: int


def build_bootstrap_knowledge(
    *,
    session: AsyncSession,
    workspace_id: uuid.UUID,
    region: str,
    settings: Settings | None = None,
    redis_client: Any = None,
    progress_subscriber: Any = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Knowledge | None:
    """Build the workspace's :class:`Knowledge` facade for the bootstrap path.

    Routes through the resolver for caller_id ``knowledge.ingest``
    (same path the settle extractor takes). Returns ``None`` (soft-fail)
    when the resolver finds no match — the bootstrap pipeline marks the
    run ``failed:ingest`` rather than guessing a model.

    The returned facade hangs an ``ingest_callable`` over
    :class:`IngestCompiler.compile_batch` with a per-session
    :class:`_ResolverCompileLlm` over the resolver (the same plumbing the
    settle extractor factory uses, so a workspace's LLM swap propagates
    here automatically).
    The settle / retrieve callables are stubs (the bootstrap doesn't drive
    them) — the facade is intentionally narrow for this caller.

    ``redis_client`` (Lift E8 Bug 1) is threaded into the resolver so an
    executor adapter has a transport for the worker stream XADD. Without
    it, an executor account returned by the resolver raises
    :class:`~backend.dispatch.adapter.ExecutorAdapterUnavailable` on its
    first chat call, the IngestCompiler chunk loop catches and counts the
    failure, and every chunk drops silently — exactly the qazasa123
    dogfood symptom that surfaced this lift.

    ``progress_subscriber`` (Lift E9) is an
    :class:`~backend.knowledge._internal.events.EventSubscriber` (typically
    :class:`_BootstrapProgressSubscriber`) that listens for the
    ``INGEST_COMPILE_BATCH_*`` event family and surfaces per-chunk
    progress onto the product row. ``None`` is non-fatal — the bootstrap
    still runs end-to-end, the founder UI just falls back to the
    status pill.

    ``session_factory`` (Lift E19) is threaded into the resolver so the
    :class:`~backend.dispatch.adapter.ExecutorAdapter` opens a fresh
    ``AsyncSession`` per ``chat`` call. Without it, parallel chunks of
    :meth:`IngestCompiler.compile_batch` (Lift E18 fan-out) share the
    same session and hit ``Session is already flushing`` on
    ``dispatch.create_task`` / ``dispatch.dispatch_task``. The
    bootstrap orchestrator already holds the sessionmaker — pass it
    through.
    """
    settings = settings or get_settings()
    return _build_bootstrap_knowledge_inner(
        session=session,
        workspace_id=workspace_id,
        region=region,
        settings=settings,
        redis_client=redis_client,
        progress_subscriber=progress_subscriber,
        session_factory=session_factory,
    )


def _build_bootstrap_knowledge_inner(
    *,
    session: AsyncSession,
    workspace_id: uuid.UUID,
    region: str,
    settings: Settings,
    redis_client: Any = None,
    progress_subscriber: Any = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Knowledge | None:
    # Lazy imports keep the runtime layer's top-level cheap and avoid the
    # heavy Knowledge subsystem at module load.
    from backend.knowledge._internal.events import EventBus  # noqa: PLC0415
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
    ) -> _IngestCallResult:
        # Lift E8 Bug 1 — thread ``redis_client`` so an executor adapter
        # the resolver returns has a transport for the worker stream XADD.
        # Without it, ExecutorAdapter.chat raises ExecutorAdapterUnavailable
        # on the first chunk and the IngestCompiler silently drops every
        # chunk into the chunk_failures counter.
        resolved = await _resolve_via_caller(
            session,
            caller_id=CALLER_KNOWLEDGE_INGEST,
            workspace_id=workspace_id,
            settings=settings,
            redis=redis_client,
            # Lift E19 — each parallel chunk of IngestCompiler.compile_batch
            # (Lift E18 asyncio.gather fan-out) needs its OWN AsyncSession
            # for the dispatch lifecycle, or two concurrent
            # ``dispatch.create_task`` / ``dispatch.dispatch_task`` calls
            # race on ``session.flush()`` and raise
            # ``InvalidRequestError: Session is already flushing``.
            session_factory=session_factory,
        )
        if resolved is None:
            logger.info(
                "bootstrap_ingest_account_unresolved",
                workspace_id=str(workspace_id),
                caller_id=CALLER_KNOWLEDGE_INGEST,
            )
            return _IngestCallResult(notes_created=0, notes_updated=0, chunk_failures=0)
        llm = _ResolverCompileLlm(adapter=resolved.adapter)
        factory = KnowledgeFactory(
            region=region,
            workspace_id=str(workspace_id),
            vault_root=Path(settings.knowledge_vault_root),
        )
        canon_service = await _build_canonicalization_service(workspace_id, region)
        # Lift E9 — wire the progress subscriber onto a fresh EventBus so
        # the compiler emits ``INGEST_COMPILE_BATCH_*`` events into our
        # surface. ``None`` subscriber → still build the bus (zero-cost
        # no-op without subscribers) so the compiler doesn't branch on
        # whether the runtime opted into progress.
        bus = EventBus()
        if progress_subscriber is not None:
            bus.subscribe(progress_subscriber)
        compiler = IngestCompiler(
            garden_writer=factory.writer(),
            llm_client=llm,
            canonicalization_service=canon_service,
            event_bus=bus,
            parallelism=settings.ingest_compile_parallelism,
        )
        items = [
            BatchItem(label=str(a.get("label", "")), content=str(a.get("content", "")))
            for a in artifacts
        ]
        result = await compiler.compile_batch(items, seed_source="product-bootstrap")
        return _IngestCallResult(
            notes_created=result.notes_created,
            notes_updated=result.notes_updated,
            # ``CompileResult`` carries ``chunk_failures`` through the per-batch
            # analytics record (:class:`IngestBatchRecord`). The current
            # ``CompileResult`` dataclass does not expose it as a field, so we
            # mirror what the compiler logged via ``getattr`` with a 0 default
            # for forward compatibility — the field is being added in this lift.
            chunk_failures=int(getattr(result, "chunk_failures", 0) or 0),
        )

    async def _settle_stub() -> int:
        return 0

    async def _retrieve_stub(**_: object) -> list[dict[str, object]]:
        return []

    # Local concrete that satisfies the ``Knowledge`` Protocol without
    # importing the SqlAlchemyKnowledge concrete (which would pull in the
    # CanonRetriever construction wiring the bootstrap doesn't need).
    class _BootstrapKnowledge:
        async def ingest(self, request: IngestRequest) -> IngestResult:
            call_result = await _ingest_callable(
                workspace_id=request.workspace_id,
                region=request.region,
                artifacts=list(request.artifacts),
            )
            return IngestResult(
                proposals_count=0,
                notes_count=call_result.notes_created + call_result.notes_updated,
                run_id=uuid.uuid5(uuid.NAMESPACE_URL, f"bootstrap:{request.workspace_id}"),
                # Lift E8 Bug 2 — surface the failure signal so the runtime
                # layer can mark ``failed:ingest`` when every chunk dropped.
                notes_created=call_result.notes_created,
                notes_updated=call_result.notes_updated,
                chunk_failures=call_result.chunk_failures,
            )

        async def retrieve_canon(self, query: Any) -> CanonRetrievalResult:
            del query
            return CanonRetrievalResult(notes=[])

        async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int:
            del workspace_id, region
            return await _settle_stub()

    return _BootstrapKnowledge()


async def _reconcile_embeddings_soft(
    *,
    workspace_id: uuid.UUID,
    region: str,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Embed the freshly imported knowledge so the project is retrievable.

    The bootstrap module writes garden seedlings + concept anchors but has no
    embedding step of its own, so a fresh import was un-retrievable until a
    manual reconcile (the import-pipeline K3 gap). This runs Lift 3's idempotent
    :func:`~backend.knowledge.retrieval.reconcile.reconcile_embeddings` over the
    workspace vault once ingest succeeds. Soft-fail + own session: a missing or
    failed embedder never reverts the completed bootstrap; no-op when no
    embedding model is configured."""
    from pathlib import Path  # noqa: PLC0415

    from backend.knowledge.graph.vault import Vault  # noqa: PLC0415
    from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
        resolve_knowledge_embedder,
    )
    from backend.knowledge.retrieval.reconcile import reconcile_embeddings  # noqa: PLC0415
    from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend  # noqa: PLC0415

    try:
        embedder = resolve_knowledge_embedder(settings)
        if not embedder.enabled or embedder.model is None:
            return
        vault = Vault(Path(settings.knowledge_vault_root) / region / str(workspace_id))
        async with session_factory() as session:
            backend = PgNoteVectorBackend(
                session, workspace_id=workspace_id, embedding_model=embedder.model
            )
            result = await reconcile_embeddings(vault, embedder, backend)
            await session.commit()
        logger.info(
            "bootstrap_embeddings_reconciled",
            workspace_id=str(workspace_id),
            embedded=getattr(result, "embedded", None),
            scanned=getattr(result, "scanned", None),
        )
    except Exception:  # noqa: BLE001 — embedding is derived; never revert a completed bootstrap
        logger.warning(
            "bootstrap_embeddings_reconcile_failed",
            workspace_id=str(workspace_id),
            exc_info=True,
        )


async def _scaffold_gate_if_missing(repo_path: Path, git_ops: GitOps) -> None:
    """I1c — scaffold a minimal acceptance gate when the repo declares none.

    :func:`scaffold_gate` is a no-op (returns ``None``) when the repo already
    has its own gate, a ``ci.yml`` already exists, or the stack is unknown, so
    this never clobbers a real CI. Best-effort: any write/commit failure is
    logged and swallowed — a missing gate only weakens the honesty grade and
    must never fail the bootstrap."""
    try:
        gate = scaffold_gate(repo_path)
        if gate is None:
            return
        target = repo_path / gate.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(gate.content, encoding="utf-8")
        committed = await git_ops.commit_all(
            repo_path, "chore(ci): scaffold minimal acceptance gate (BSVibe)"
        )
        logger.info(
            "gate_scaffolded",
            repo=str(repo_path),
            stack=gate.stack,
            committed=committed,
        )
    except Exception as exc:  # noqa: BLE001 — scaffolding must never break bootstrap
        logger.warning("gate_scaffold_failed", repo=str(repo_path), error=str(exc))


async def run_product_bootstrap_job(  # noqa: PLR0915 — linear clone→scaffold→ingest job
    *,
    product_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    repo: BootstrapRepository | None = None,
    git_ops: GitOps | None = None,
    redis_client: Any = None,
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

    # I1c — the target's OWN gate is what I1 verifies against. A cloned repo
    # that declares no gate (notably a Python project — discover_gate has no
    # pyproject detector) would leave "verified" at the weakest honesty grade,
    # so scaffold a minimal CI for its stack and commit it to main. Best-effort:
    # it never fails the bootstrap.
    await _scaffold_gate_if_missing(repo_path, git_ops)

    await repo.mark_status(product_id, status=STATUS_ANALYZING)

    # Lift E9 — wire a per-job progress subscriber so the compile_batch
    # event stream lands on the product row. The subscriber holds the
    # session FACTORY (not a session) so each write opens a fresh
    # short-lived session and never blocks other workspaces' writes.
    progress_subscriber = _BootstrapProgressSubscriber(
        session_factory=session_factory,
        product_id=product_id,
    )

    async with session_factory() as session:
        knowledge = build_bootstrap_knowledge(
            session=session,
            workspace_id=workspace_id,
            region=region,
            settings=settings,
            redis_client=redis_client,
            progress_subscriber=progress_subscriber,
            # Lift E19 — thread the bootstrap orchestrator's sessionmaker
            # all the way down to the ExecutorAdapter so each parallel
            # chunk of compile_batch (E18 fan-out) gets its own session.
            session_factory=session_factory,
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
                # Lift E20 — the orchestrator persists the code graph
                # to ``<vault_root>/code_graph/graph.json`` so the MCP
                # graph query surface can serve it later.
                vault_root=Path(settings.knowledge_vault_root) / region / str(workspace_id),
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

    # Lift E8 Bug 2 — decide ``failed:ingest`` vs ``complete`` based on
    # whether ingest actually produced ANY notes. Before this lift the
    # runtime cheerfully marked ``complete`` whenever the orchestrator
    # returned an outcome — even when EVERY chunk had dropped to a
    # transport error (the qazasa123 dogfood showed artifacts_count=1377
    # / notes_count=0 / status=complete, leaving the founder UI saying
    # "all good" with an empty knowledge graph).
    notes_written = outcome.notes_written
    chunk_failures = outcome.chunk_failures
    if notes_written == 0 and chunk_failures > 0:
        # Every chunk dropped — the ingest never succeeded for ANY artifact.
        chunk_total = chunk_failures  # all chunks that ran failed
        error_msg = (
            f"ingest failed: {chunk_failures}/{chunk_total} chunks raised — "
            f"0 notes written from {outcome.artifacts_count} artifacts"
        )
        await repo.mark_status(
            product_id,
            status=STATUS_FAILED_INGEST,
            run_id=run_id,
            error=error_msg,
        )
        logger.warning(
            _AUDIT_FAILED,
            product_id=str(product_id),
            workspace_id=str(workspace_id),
            run_id=str(run_id),
            reason="ingest_zero_notes",
            artifacts_count=outcome.artifacts_count,
            chunk_failures=chunk_failures,
        )
        return

    # Import-pipeline K3 fix — embed the knowledge ingest just wrote (the
    # bootstrap path has no embedding step of its own), so the freshly imported
    # project is immediately retrievable. Soft-fail: an embedding failure never
    # blocks marking the bootstrap complete.
    await _reconcile_embeddings_soft(
        workspace_id=workspace_id,
        region=region,
        settings=settings,
        session_factory=session_factory,
    )

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
        notes_written=notes_written,
        chunk_failures=chunk_failures,
    )


def schedule_product_bootstrap(
    *,
    product_id: uuid.UUID,
    workspace_id: uuid.UUID,
    repo_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings | None = None,
    redis_client: Any = None,
) -> asyncio.Task[None]:
    """Fire-and-forget the bootstrap job; return the task for tests.

    Holds a strong reference in :data:`_running` until completion so the
    event loop's weak-ref scheduler can't drop it. Errors inside the job
    are caught by :func:`run_product_bootstrap_job` itself — the task's
    own ``done`` callback only logs unhandled crashes for visibility.

    ``redis_client`` (Lift E8 Bug 1) is threaded into the job so executor
    accounts the resolver returns have a transport for the worker stream
    XADD. When the caller does not supply one, the function lazily builds
    a Redis client from ``settings.redis_url`` (mirrors the pattern in
    :func:`backend.workflow.application.runtime.lifecycle.run_workers`) —
    so the production bootstrap path picks up Redis automatically.
    Failure to construct the client is non-fatal: the bootstrap still
    runs, and a LiteLLM-backed account works without Redis.
    """
    resolved_settings = settings or get_settings()
    if redis_client is None:
        redis_client = _build_redis_client(resolved_settings)
    task = asyncio.create_task(
        run_product_bootstrap_job(
            product_id=product_id,
            workspace_id=workspace_id,
            repo_url=repo_url,
            session_factory=session_factory,
            settings=resolved_settings,
            redis_client=redis_client,
        )
    )
    _running.add(task)
    # Lift E13 — index by product_id so the cancel tool can find the
    # task and ``task.cancel()`` it without having to scan ``_running``.
    _running_by_product[product_id] = task
    task.add_done_callback(_on_task_done)
    return task


def _build_redis_client(settings: Settings) -> Any:
    """Build a ``redis.asyncio`` client from ``settings.redis_url``.

    Returns ``None`` when no URL is configured OR construction fails — both
    are non-fatal: the bootstrap still proceeds, and a LiteLLM-backed
    account never touches Redis. An executor-backed account will surface
    its own ``ExecutorAdapterUnavailable`` later, which the runtime now
    propagates to ``failed:ingest`` via Bug 2's chunk-failure gate.
    """
    if not settings.redis_url:
        return None
    try:
        import redis.asyncio as redis_aio  # noqa: PLC0415 — only on bootstrap path

        return redis_aio.from_url(settings.redis_url, decode_responses=True)
    except Exception:  # noqa: BLE001 — Redis is optional for the LiteLLM path
        logger.warning(
            "bootstrap_redis_connect_failed",
            redis_url=redact_url_password(settings.redis_url),
            exc_info=True,
        )
        return None


def _on_task_done(task: asyncio.Task[None]) -> None:
    _running.discard(task)
    # Lift E13 — drop the by-product index entry only if it still points
    # at this task. (A retry that scheduled a NEW task for the same
    # product_id will have already replaced the entry; we must not yank
    # the new task out from under itself.)
    for pid, t in list(_running_by_product.items()):
        if t is task:
            _running_by_product.pop(pid, None)
            break
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
    "_BootstrapProgressSubscriber",
    "build_bootstrap_knowledge",
    "get_running_task",
    "register_running_task",
    "run_product_bootstrap_job",
    "schedule_product_bootstrap",
    "unregister_running_task",
]
