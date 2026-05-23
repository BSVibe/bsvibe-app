"""SettleWorker — drain ``settle`` activities into the BSage graph.

Workflow §4: ``worker-settle`` is the **BSage write subscriber**. The agent
loop (:class:`backend.execution.orchestrator.RunOrchestrator`) records a
``settle``-class :class:`~backend.execution.db.ExecutionRunActivity` for every
verified work step — the continuous "writes knowledge" side channel. This
worker drains those rows into each workspace's BSage vault, completing the
*learning half* of the §5 trust ratchet (BSage knowledge is one-way and
monotonically accumulates).

Design notes
------------
* **DB-polling, not Redis Streams** — same Phase 1 justification as
  :class:`~backend.workers.delivery_worker.DeliveryWorker`. The Redis
  Streams variant is deferred.
* **Idempotent via a marker table, not a deletable queue.** Unlike
  ``delivery_events``, the source ``execution_run_activities`` rows are
  append-only telemetry the run-trace UI reads — we must not consume them.
  Each absorbed activity gets a :class:`~backend.workers.db.SettleDrainRow`
  (keyed by ``activity_id``); the drain query skips ids already marked, so
  ``drain_once`` called twice writes nothing the second time. The marker is
  committed per-row right after its write, so a crash mid-batch can at worst
  re-write the single in-flight activity (at-least-once), never the batch.
* **Workspace isolation is structural.** The drain query runs with the §3
  workspace contextvar unset (the documented no-op path for background
  workers), so it sees every workspace's settle activities. Each write is
  then routed through a :class:`~backend.knowledge.factory.KnowledgeFactory`
  bound to *that activity's* ``workspace_id`` — the factory roots the vault
  at ``<vault_root>/<region>/<workspace_id>/``, so a settle for workspace A
  can never land in workspace B's vault.
* **Promotion closes the §5 ratchet loop.** Depositing the raw observation is
  only the *learning* half. After a drain batch writes garden notes, the
  worker runs a :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
  per *affected* workspace so recurring patterns get promoted into canonical
  anchors (the "wall"). The promoter is idempotent and honours the
  workspace's canonicalization policy (Safe-Mode default → queued proposals;
  permissive → applied anchors), so a per-batch run is safe. Promotion is the
  *derived* step: a promotion failure is soft (logged + skipped) and never
  reverts a settle write or breaks the drain — the settlement notes remain the
  source of truth. The promoter is constructed against the SAME per-workspace
  vault boundary the sink uses (``<vault_root>/<region>/<workspace_id>/`` via
  the :class:`~backend.knowledge.factory.KnowledgeFactory` convention) behind a
  :class:`PromoterFactory` seam so the binding stays testable.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.execution.db import ExecutionRunActivity
from backend.workers.base import BaseWorker
from backend.workers.db import SettleDrainRow
from backend.workspaces.db import WorkspaceRow

logger = structlog.get_logger(__name__)

SETTLE_ACTIVITY_TYPE = "settle"


@dataclass(frozen=True, slots=True)
class Settlement:
    """One verified-work observation to deposit into a workspace's BSage graph.

    Built by the worker from a ``settle`` activity row; the ``region`` is
    resolved from the workspace so the sink stays a pure writer with no DB
    access of its own.
    """

    workspace_id: uuid.UUID
    region: str
    run_id: uuid.UUID
    activity_id: uuid.UUID
    verified: bool
    summary: str
    artifact_refs: list[str] = field(default_factory=list)
    occurred_at: datetime | None = None


class SettleSink(Protocol):
    """Absorbs a :class:`Settlement` into knowledge storage.

    Returns a reference to the written node (a vault path) or ``None`` when
    nothing was written. The worker is decoupled from BSage behind this
    Protocol so the drain bookkeeping is testable without a real vault.
    """

    async def absorb(self, settlement: Settlement) -> str | None: ...


class WorkspacePromoter(Protocol):
    """Promotes a workspace's accumulated garden observations into canon.

    Mirrors the surface of
    :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
    that the worker depends on (``promote``), so the post-drain promotion step
    is testable without standing up a real canonicalization engine.
    """

    async def promote(self) -> object: ...


class PromoterFactory(Protocol):
    """Builds a :class:`WorkspacePromoter` for one workspace/region.

    Construction is the per-workspace vault boundary: the promoter is rooted at
    ``<vault_root>/<region>/<workspace_id>/`` (the
    :class:`~backend.knowledge.factory.KnowledgeFactory` convention), reusing
    the exact boundary the sink wrote to. ``safe_mode`` carries the workspace's
    canonicalization policy through to the engine (default strict → queued;
    permissive → applied). Returning ``None`` disables promotion for that
    workspace (e.g. promotion not configured).
    """

    def __call__(
        self, *, region: str, workspace_id: uuid.UUID, safe_mode: bool
    ) -> WorkspacePromoter | None: ...


@dataclass(slots=True)
class SettleWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0
    # Fallback region when a workspace row carries none (matches
    # ``Settings.knowledge_default_region``). The runtime entrypoint should
    # pass the configured value.
    default_region: str = "us-1"


class KnowledgeSettleSink:
    """Production sink — writes each settlement as a BSage garden observation.

    The settle observation is the §5 ratchet deposit: a verified work step
    leaves a one-way, monotonically-accumulating knowledge sample.
    Canonicalization (a separate subscriber/cron) later promotes repeatedly
    observed patterns into canonical nodes — that pipeline is out of scope
    here; this sink only deposits the raw observation via the writer.
    """

    __slots__ = ("_vault_root",)

    def __init__(self, *, vault_root: Path) -> None:
        self._vault_root = vault_root

    async def absorb(self, settlement: Settlement) -> str | None:
        from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415 — lazy heavy import
        from backend.knowledge.graph.writer import GardenNote  # noqa: PLC0415

        factory = KnowledgeFactory(
            region=settlement.region,
            workspace_id=str(settlement.workspace_id),
            vault_root=self._vault_root,
        )
        writer = factory.writer()

        summary = settlement.summary.strip()
        # Title is descriptive only — the drain marker (keyed on activity_id)
        # owns system identity, so a thin/odd LLM summary can't break dedup.
        headline = summary.splitlines()[0][:80] if summary else "verified work step"
        note = GardenNote(
            title=f"Settle: {headline}",
            content=_observation_body(settlement, summary),
            source="settle_worker",
            knowledge_layer="episodic",
            tags=["settle", "verified-run"],
            extra_fields={
                "run_id": str(settlement.run_id),
                "activity_id": str(settlement.activity_id),
                "verified": settlement.verified,
                "artifact_refs": list(settlement.artifact_refs),
            },
        )
        path = await writer.write_garden(note)
        return str(path)


def _observation_body(settlement: Settlement, summary: str) -> str:
    lines = [summary or "(no summary recorded)", ""]
    if settlement.artifact_refs:
        lines.append("## Artifacts")
        lines.extend(f"- `{ref}`" for ref in settlement.artifact_refs)
        lines.append("")
    lines.append(f"Verified: {'yes' if settlement.verified else 'no'}")
    lines.append(f"Run: {settlement.run_id}")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class _WorkspacePolicy:
    """Per-workspace settle context resolved from the ``workspaces`` row.

    ``safe_mode`` is the workspace's canonicalization policy (Safe-Mode default
    True → promotion queues proposals; False → it auto-applies anchors).
    """

    region: str
    safe_mode: bool


def build_garden_promoter_factory(*, vault_root: Path) -> PromoterFactory:
    """Production :class:`PromoterFactory` rooted at the shared vault boundary.

    Builds, per call, a :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
    over a :class:`~backend.knowledge.graph.storage.FileSystemStorage` rooted at
    ``<vault_root>/<region>/<workspace_id>/`` — the exact directory
    :class:`KnowledgeSettleSink` (via :class:`~backend.knowledge.factory.KnowledgeFactory`)
    wrote the garden observations to. ``safe_mode`` is threaded into the
    :class:`~backend.knowledge.canonicalization.service.CanonicalizationService`
    so the workspace's policy decides queue-vs-apply; the worker never overrides
    it. The returned factory is sync but builds a coroutine-driven promoter, so
    promotion itself runs inside the worker's ``await``.
    """

    def _factory(
        *, region: str, workspace_id: uuid.UUID, safe_mode: bool
    ) -> WorkspacePromoter | None:
        return _LazyGardenPromoter(
            vault_root=vault_root,
            region=region,
            workspace_id=workspace_id,
            safe_mode=safe_mode,
        )

    return _factory


class _LazyGardenPromoter:
    """Adapter that builds the canonicalization engine on first ``promote``.

    The engine (storage + index rebuild + service) is constructed inside the
    coroutine so the heavy canonicalization imports stay lazy (the worker module
    must import cheaply) and the per-workspace index is freshly rebuilt from the
    just-written vault state each pass — keeping promotion idempotent.
    """

    __slots__ = ("_vault_root", "_region", "_workspace_id", "_safe_mode")

    def __init__(
        self, *, vault_root: Path, region: str, workspace_id: uuid.UUID, safe_mode: bool
    ) -> None:
        self._vault_root = vault_root
        self._region = region
        self._workspace_id = workspace_id
        self._safe_mode = safe_mode

    async def promote(self) -> object:
        # Lazy heavy imports — keep the worker entrypoint cheap.
        from backend.knowledge.canonicalization.index import (  # noqa: PLC0415
            InMemoryCanonicalizationIndex,
        )
        from backend.knowledge.canonicalization.lock import AsyncIOMutationLock  # noqa: PLC0415
        from backend.knowledge.canonicalization.promotion import (  # noqa: PLC0415
            GardenObservationPromoter,
        )
        from backend.knowledge.canonicalization.resolver import TagResolver  # noqa: PLC0415
        from backend.knowledge.canonicalization.service import (  # noqa: PLC0415
            CanonicalizationService,
        )
        from backend.knowledge.canonicalization.store import NoteStore  # noqa: PLC0415
        from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415

        # SAME boundary the sink wrote to: <vault_root>/<region>/<workspace_id>/.
        ws_root = self._vault_root / self._region / str(self._workspace_id)
        ws_root.mkdir(parents=True, exist_ok=True)
        storage = FileSystemStorage(ws_root)

        index = InMemoryCanonicalizationIndex()
        await index.initialize(storage)
        safe_mode = self._safe_mode
        service = CanonicalizationService(
            store=NoteStore(storage),
            lock=AsyncIOMutationLock(),
            index=index,
            resolver=TagResolver(index=index),
            safe_mode=lambda: safe_mode,
        )
        return await GardenObservationPromoter(service).promote()


class SettleWorker(BaseWorker):
    """Periodic drain of ``settle`` activities into BSage (the §4 write subscriber)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sink: SettleSink,
        config: SettleWorkerConfig | None = None,
        promoter_factory: PromoterFactory | None = None,
    ) -> None:
        self._cfg = config or SettleWorkerConfig()
        super().__init__(name="settle_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._sink = sink
        # When unset, promotion is disabled (e.g. tests exercising only the
        # drain bookkeeping). The runtime entrypoint wires the default factory.
        self._promoter_factory = promoter_factory

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Absorb a batch of un-drained ``settle`` activities, then promote.

        Returns the count of activities absorbed. After the batch, the
        canonical-pattern promoter runs once per *affected* workspace (those
        with ≥1 successful absorb this batch) to close the §5 ratchet loop.
        Promotion is soft-fail: an error is logged and skipped, never reverting
        a settle write or affecting the returned drain count.
        """
        async with self._session_factory() as session:
            rows = await self._claim_undrained(session)
            if not rows:
                return 0
            policies = await self._resolve_workspaces(session, {r.workspace_id for r in rows})

            processed = 0
            promoted_ids: set[uuid.UUID] = set()
            for row in rows:
                policy = policies.get(
                    row.workspace_id, _WorkspacePolicy(self._cfg.default_region, True)
                )
                settlement = _to_settlement(row, policy.region)
                try:
                    node_ref = await self._sink.absorb(settlement)
                except Exception:  # noqa: BLE001 — record + leave un-drained for retry
                    logger.exception(
                        "settle_worker_absorb_failed",
                        activity_id=str(row.id),
                        workspace_id=str(row.workspace_id),
                    )
                    continue
                # Mark drained per-row + commit immediately: a later crash can
                # only re-write the next in-flight activity, never this batch.
                session.add(
                    SettleDrainRow(
                        activity_id=row.id,
                        workspace_id=row.workspace_id,
                        run_id=row.run_id,
                        node_ref=node_ref,
                        drained_at=datetime.now(tz=UTC),
                    )
                )
                await session.commit()
                processed += 1
                promoted_ids.add(row.workspace_id)
                logger.info(
                    "settle_worker_absorbed",
                    activity_id=str(row.id),
                    workspace_id=str(row.workspace_id),
                    node_ref=node_ref,
                )

            # Close the loop: promote each affected workspace's accumulated
            # observations into canon. Derived + soft-fail — never affects the
            # drain count or reverts a write above.
            await self._promote_affected(promoted_ids, policies)
            return processed

    async def _promote_affected(
        self,
        workspace_ids: set[uuid.UUID],
        policies: dict[uuid.UUID, _WorkspacePolicy],
    ) -> None:
        """Run the canonical-pattern promoter for each affected workspace.

        Per-workspace, idempotent, policy-honoring, and soft-fail: each
        promotion is isolated so one workspace's failure can't stop another's,
        and no promotion error reverts the settlement writes (the SoT).
        """
        if self._promoter_factory is None:
            return
        for workspace_id in sorted(workspace_ids, key=str):
            policy = policies.get(workspace_id, _WorkspacePolicy(self._cfg.default_region, True))
            try:
                promoter = self._promoter_factory(
                    region=policy.region,
                    workspace_id=workspace_id,
                    safe_mode=policy.safe_mode,
                )
                if promoter is None:
                    continue
                await promoter.promote()
            except Exception:  # noqa: BLE001 — promotion is derived; never break the drain
                logger.exception(
                    "settle_worker_promotion_failed",
                    workspace_id=str(workspace_id),
                    safe_mode=policy.safe_mode,
                )
                continue
            logger.info(
                "settle_worker_promotion_complete",
                workspace_id=str(workspace_id),
                safe_mode=policy.safe_mode,
            )

    async def _claim_undrained(self, session: AsyncSession) -> list[ExecutionRunActivity]:
        already_drained = select(SettleDrainRow.activity_id)
        stmt = (
            select(ExecutionRunActivity)
            .where(
                ExecutionRunActivity.activity_type == SETTLE_ACTIVITY_TYPE,
                ExecutionRunActivity.id.notin_(already_drained),
            )
            .order_by(ExecutionRunActivity.created_at.asc())
            .limit(self._cfg.batch_size)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _resolve_workspaces(
        self, session: AsyncSession, workspace_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, _WorkspacePolicy]:
        """Resolve region + canonicalization policy for each workspace.

        A missing row (telemetry can outlive its workspace) falls back to the
        configured default region + the strict Safe-Mode default — never
        auto-applying canon for an unknown workspace.
        """
        ids = list(workspace_ids)
        if not ids:
            return {}
        stmt = select(WorkspaceRow.id, WorkspaceRow.region, WorkspaceRow.safe_mode).where(
            WorkspaceRow.id.in_(ids)
        )
        return {
            wid: _WorkspacePolicy(region, bool(safe_mode))
            for wid, region, safe_mode in (await session.execute(stmt)).all()
        }


def _to_settlement(row: ExecutionRunActivity, region: str) -> Settlement:
    payload = row.payload or {}
    refs = payload.get("artifact_refs") or []
    return Settlement(
        workspace_id=row.workspace_id,
        region=region,
        run_id=row.run_id,
        activity_id=row.id,
        verified=bool(payload.get("verified", False)),
        summary=str(payload.get("summary") or ""),
        artifact_refs=[str(r) for r in refs],
        occurred_at=row.created_at,
    )


__all__ = [
    "KnowledgeSettleSink",
    "PromoterFactory",
    "SettleSink",
    "SettleWorker",
    "SettleWorkerConfig",
    "Settlement",
    "WorkspacePromoter",
    "build_garden_promoter_factory",
]
