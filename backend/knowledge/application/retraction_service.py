"""RetractionService — the application-layer orchestrator for ontology corrections.

Lift M3a. Three load-bearing operations:

* :meth:`issue` — handler intake. Persists the
  :class:`~backend.knowledge.domain.retraction.RetractionSignal` to
  ``ontology_corrections``, emits the ``ontology.correction.requested``
  audit event, and opens the 30-second undo window. Idempotent on
  ``signal.id``.
* :meth:`undo` — founder pressed Undo. Sets ``cancelled_at`` if still in
  window; emits ``ontology.correction.undone``. After :attr:`apply_at`
  passes, returns ``expired`` and writes nothing.
* :meth:`apply_pending` — lazy resolver / sweep. Finds rows past
  ``apply_at`` that are neither applied nor cancelled and commits the
  tombstone (writes ``retracted_at`` to the note's frontmatter via
  :class:`GardenWriter`). Sets ``applied_at`` and emits
  ``ontology.correction.applied``.

The service treats the DB row as the timer — no process-local sleep, no
in-memory cache. A process restart between intake and apply does not lose
the window because :meth:`apply_pending` is the same check whether it
fires from a sweep, a follow-up request, or the next worker tick.

Vault writes go through the existing
:class:`~backend.knowledge.graph.writer_core._mutation._WriterMutationMixin.tombstone_note`
method on :class:`GardenWriter`, which reuses the per-workspace
``_garden_lock`` — so a retraction never interleaves with a concurrent
canon-merge or settle write.

The retract action lands tombstones on garden notes; canonical-concept
retract (which the design routes through the existing
``deprecate-concept`` canon action) is OUT of scope for M3a — only the
garden-note tombstone path is wired here. ``correct`` is an in-place
field rewrite (``question`` / ``answer`` whitelist) with no undo window,
per design §3.3.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.knowledge.application.audit_events import (
    OntologyCorrectionApplied,
    OntologyCorrectionRequested,
    OntologyCorrectionUndone,
)
from backend.knowledge.domain.retraction import (
    UNDO_WINDOW_SECONDS,
    OntologyAction,
    RetractionSignal,
)
from backend.knowledge.infrastructure.ontology_db import OntologyCorrection
from plugin.audit.events import AuditActor
from plugin.audit.service import safe_emit

logger = structlog.get_logger(__name__)


UndoResult = Literal["undone", "expired", "already_applied", "already_undone", "not_found"]


def _as_utc(value: datetime) -> datetime:
    """Force a tz-aware UTC datetime — SQLite drops the offset on round-trip
    even when the column is ``DateTime(timezone=True)``. Treating a naive
    timestamp as UTC keeps the comparison stable across PG (aware) + SQLite
    (naive) without changing the on-disk wire."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class TombstoneWriter(Protocol):
    """Minimal surface this service needs from the vault writer.

    Structural Protocol — the production :class:`GardenWriter` satisfies it
    via its :class:`_WriterMutationMixin` methods. Tests pass a small fake.
    """

    async def tombstone_note(
        self,
        path: str,
        *,
        retracted_at: str,
        retracted_by: str,
        retraction_reason: str | None = None,
    ) -> object: ...

    async def restore_note_from_tombstone(self, path: str) -> object: ...


class RetractionService:
    """Orchestrates the issue → undo-window → apply lifecycle.

    Constructor-injected with an :class:`AsyncSession` (the request /
    worker tick owns the transaction) and a :class:`TombstoneWriter` (the
    per-workspace vault writer). The service never opens its own
    transaction and never holds a session beyond the call site.
    """

    def __init__(self, session: AsyncSession, writer: TombstoneWriter) -> None:
        self._session = session
        self._writer = writer

    # --- Issue --------------------------------------------------------

    async def issue(
        self,
        *,
        workspace_id: uuid.UUID,
        actor_id: uuid.UUID,
        node_ref: str,
        action: OntologyAction,
        reason: str | None = None,
        correction_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> tuple[RetractionSignal, bool]:
        """Persist a new correction; return ``(signal, created)``.

        Idempotent on ``correction_id`` — if a row already exists for the
        supplied id, returns the persisted signal and ``created=False``
        with no audit re-emit. When ``correction_id`` is ``None``, a fresh
        uuid is minted and a new row is created.

        Caller (the REST handler) is responsible for: (1) RBAC checks
        before calling, (2) ``node_ref`` existence check (so a 404 is
        returned instead of an orphan correction row), (3) commit.
        """
        issued_at = (now or datetime.now(tz=UTC)).astimezone(UTC)
        apply_at = issued_at + timedelta(seconds=UNDO_WINDOW_SECONDS)
        cid = correction_id or uuid.uuid4()

        # Idempotency check — re-issue on same id returns the existing row.
        existing = await self._session.get(OntologyCorrection, cid)
        if existing is not None:
            return self._signal_from_row(existing), False

        signal = RetractionSignal(
            id=cid,
            workspace_id=workspace_id,
            actor_id=actor_id,
            node_ref=node_ref,
            action=action,
            issued_at=issued_at,
            apply_at=apply_at,
            reason=reason,
        )
        row = OntologyCorrection(
            id=signal.id,
            workspace_id=signal.workspace_id,
            actor_id=signal.actor_id,
            action=signal.action,
            node_ref=signal.node_ref,
            reason=signal.reason,
            signal_json=signal.model_dump(mode="json"),
            issued_at=signal.issued_at,
            apply_at=signal.apply_at,
        )
        self._session.add(row)
        await self._session.flush()

        await self._emit_audit(
            OntologyCorrectionRequested,
            signal,
        )
        logger.info(
            "ontology_correction_issued",
            correction_id=str(signal.id),
            workspace_id=str(signal.workspace_id),
            action=signal.action,
            node_ref=signal.node_ref,
        )
        return signal, True

    # --- Undo ---------------------------------------------------------

    async def undo(
        self,
        *,
        correction_id: uuid.UUID,
        workspace_id: uuid.UUID,
        now: datetime | None = None,
    ) -> UndoResult:
        """Try to undo a correction. Returns the terminal status string.

        Terminal returns:

        * ``undone`` — happy path; row updated, audit event emitted.
        * ``expired`` — undo window has already passed.
        * ``already_applied`` — apply already ran.
        * ``already_undone`` — undo was already recorded.
        * ``not_found`` — no such row in this workspace.
        """
        ts = (now or datetime.now(tz=UTC)).astimezone(UTC)
        row = await self._session.get(OntologyCorrection, correction_id)
        if row is None or row.workspace_id != workspace_id:
            return "not_found"
        if row.applied_at is not None:
            return "already_applied"
        if row.cancelled_at is not None:
            return "already_undone"
        if ts >= _as_utc(row.apply_at):
            return "expired"
        row.cancelled_at = ts
        await self._session.flush()
        await self._emit_audit(
            OntologyCorrectionUndone,
            self._signal_from_row(row),
        )
        logger.info(
            "ontology_correction_undone",
            correction_id=str(row.id),
            workspace_id=str(row.workspace_id),
        )
        return "undone"

    # --- Apply --------------------------------------------------------

    async def apply_pending(
        self,
        *,
        workspace_id: uuid.UUID,
        now: datetime | None = None,
    ) -> int:
        """Apply every pending correction for a workspace past its deadline.

        Lazy resolver — call from any path that knows the workspace (REST
        handler tail, next worker tick). Returns the number of corrections
        actually applied (a row in window or already-terminal is skipped).
        Idempotent: re-running over the same set is a no-op (each row's
        ``applied_at`` short-circuits subsequent calls).
        """
        ts = (now or datetime.now(tz=UTC)).astimezone(UTC)
        stmt = (
            select(OntologyCorrection)
            .where(
                OntologyCorrection.workspace_id == workspace_id,
                OntologyCorrection.applied_at.is_(None),
                OntologyCorrection.cancelled_at.is_(None),
                OntologyCorrection.apply_at <= ts,
            )
            .order_by(OntologyCorrection.issued_at)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        applied = 0
        for row in rows:
            await self._apply_row(row, now=ts)
            applied += 1
        return applied

    async def apply_one(
        self,
        *,
        correction_id: uuid.UUID,
        workspace_id: uuid.UUID,
        now: datetime | None = None,
    ) -> bool:
        """Force-apply a specific correction (testing / explicit-trigger path).

        Returns ``True`` when the row transitioned to applied; ``False`` if
        already terminal or not found.
        """
        ts = (now or datetime.now(tz=UTC)).astimezone(UTC)
        row = await self._session.get(OntologyCorrection, correction_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.applied_at is not None or row.cancelled_at is not None:
            return False
        await self._apply_row(row, now=ts)
        return True

    async def _apply_row(self, row: OntologyCorrection, *, now: datetime) -> None:
        """Write the tombstone (retract) or rewrite fields (correct), then mark applied."""
        signal = self._signal_from_row(row)
        if row.action == "retract":
            await self._writer.tombstone_note(
                row.node_ref,
                retracted_at=now.isoformat(),
                retracted_by=str(row.actor_id),
                retraction_reason=row.reason,
            )
        # ``correct`` is whitelisted-field rewrite; field-payload is carried
        # in ``signal_json["corrections"]`` when set by the (future) correct
        # surface — M3a wires the row + audit + tombstone path; the actual
        # field-rewrite editor lands with M3b alongside the PWA inline editor.
        # No-op for now (the audit row still records intent + actor).
        row.applied_at = now
        await self._session.flush()
        await self._emit_audit(OntologyCorrectionApplied, signal)
        logger.info(
            "ontology_correction_applied",
            correction_id=str(row.id),
            workspace_id=str(row.workspace_id),
            action=row.action,
            node_ref=row.node_ref,
        )

    # --- Undo (restore) on an already-applied retract --------------------

    async def restore(
        self,
        *,
        correction_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> bool:
        """Out-of-band tombstone restore for a previously-applied retract.

        Returns ``True`` when the note's frontmatter tombstone was cleared.
        Out of MVP scope for the founder UI (per design §3.4 "one-shot"
        undo), but lands here as the inverse vault primitive — useful for
        admin recovery + the M3b "edit after correct" path.
        """
        row = await self._session.get(OntologyCorrection, correction_id)
        if row is None or row.workspace_id != workspace_id:
            return False
        if row.action != "retract" or row.applied_at is None:
            return False
        await self._writer.restore_note_from_tombstone(row.node_ref)
        return True

    # --- Helpers ------------------------------------------------------

    def _signal_from_row(self, row: OntologyCorrection) -> RetractionSignal:
        return RetractionSignal(
            id=row.id,
            workspace_id=row.workspace_id,
            actor_id=row.actor_id,
            node_ref=row.node_ref,
            action=row.action,  # type: ignore[arg-type]
            issued_at=_as_utc(row.issued_at),
            apply_at=_as_utc(row.apply_at),
            reason=row.reason,
        )

    async def _emit_audit(
        self,
        event_cls: type,
        signal: RetractionSignal,
    ) -> None:
        actor = AuditActor(type="user", id=str(signal.actor_id))
        payload: Mapping[str, object] = {
            "correction_id": str(signal.id),
            "action": signal.action,
            "node_ref": signal.node_ref,
            "reason": signal.reason,
            "apply_at": signal.apply_at.isoformat(),
        }
        event = event_cls(
            actor=actor,
            workspace_id=str(signal.workspace_id),
            data=dict(payload),
        )
        await safe_emit(event, session=self._session)


__all__ = [
    "RetractionService",
    "TombstoneWriter",
    "UndoResult",
]
