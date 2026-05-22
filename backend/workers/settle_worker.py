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


class SettleWorker(BaseWorker):
    """Periodic drain of ``settle`` activities into BSage (the §4 write subscriber)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sink: SettleSink,
        config: SettleWorkerConfig | None = None,
    ) -> None:
        self._cfg = config or SettleWorkerConfig()
        super().__init__(name="settle_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._sink = sink

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Absorb a batch of un-drained ``settle`` activities. Returns count written."""
        async with self._session_factory() as session:
            rows = await self._claim_undrained(session)
            if not rows:
                return 0
            regions = await self._resolve_regions(session, {r.workspace_id for r in rows})

            processed = 0
            for row in rows:
                region = regions.get(row.workspace_id, self._cfg.default_region)
                settlement = _to_settlement(row, region)
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
                logger.info(
                    "settle_worker_absorbed",
                    activity_id=str(row.id),
                    workspace_id=str(row.workspace_id),
                    node_ref=node_ref,
                )
            return processed

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

    @staticmethod
    async def _resolve_regions(
        session: AsyncSession, workspace_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        ids = list(workspace_ids)
        if not ids:
            return {}
        stmt = select(WorkspaceRow.id, WorkspaceRow.region).where(WorkspaceRow.id.in_(ids))
        return {wid: region for wid, region in (await session.execute(stmt)).all()}


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
    "SettleSink",
    "SettleWorker",
    "SettleWorkerConfig",
    "Settlement",
]
