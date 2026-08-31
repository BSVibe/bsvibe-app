"""Run cleanup — cancel / discard a run, and cascade-cancel a product's runs.

The canonical primitives behind three surfaces:

* ``bsvibe_runs_cancel`` (MCP) — mirrors ``POST /api/v1/runs/{id}/cancel``:
  only an in-flight (OPEN / RUNNING) run can be cancelled.
* ``bsvibe_runs_discard`` (MCP) — the ``폐기`` cleanup primitive: transition ANY
  non-terminal run (incl. ``review_ready``) → CANCELLED, best-effort tombstone
  its handle-less deliverables, and best-effort remove its worktree.
* Product delete cascade — :func:`cancel_product_runs` cancels every non-terminal
  run of a product before the product row is hard-deleted, so runs are never
  orphaned (``ExecutionRun.product_id`` is a loose reference, no FK cascade).

Every status flip appends an :class:`ExecutionRunHistory` audit row — the same
record :meth:`AgentRunner.transition` writes. We replicate that minimal
transition inline (rather than importing ``AgentRunner``) so this cleanup
service — reachable from the lightweight MCP leaf surface — does not drag the
whole agent-execution engine (agent_loop → skill loader → router) into MCP's
import graph. The only target here is CANCELLED, so none of ``transition``'s
REVIEW_READY-only side effects (auto-ship, impl-spawn) apply. The caller owns
the transaction boundary (these functions ``flush`` but never ``commit``).

Deliverable retraction here is a **best-effort tombstone**, NOT plugin
compensation: a deliverable with captured ``compensation_handles`` (a delivered
external artifact) is surfaced in ``deliverables_need_compensation`` rather than
silently marked retracted — undoing the real artifact stays on the explicit
``POST /api/v1/deliverables/{id}/retract`` path (which runs ``@p.compensate``).
A handle-less deliverable (never delivered externally — the common case for an
abandoned / never-shipped run) has nothing to revert, so it is tombstoned.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.infrastructure.db import (
    DecisionStatus,
    ExecutionRun,
    ExecutionRunHistory,
    RunStatus,
)
from backend.workflow.infrastructure.delivery.db import SafeModeStatus
from backend.workflow.infrastructure.repositories import (
    SqlAlchemyDecisionRepository,
    SqlAlchemyDeliverableRepository,
    SqlAlchemyRunRepository,
    SqlAlchemySafeModeQueueRepository,
)

logger = structlog.get_logger(__name__)

#: A run in one of these states is finished — nothing to cancel / discard.
_TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.SHIPPED, RunStatus.FAILED, RunStatus.CANCELLED}
)
#: Only an in-flight run can be *cancelled* (mirrors the REST /cancel guard).
_CANCELLABLE: frozenset[RunStatus] = frozenset({RunStatus.OPEN, RunStatus.RUNNING})


#: How old an *orphan* dir (no run row at all) must be before it is reclaimed.
#: A run whose row is not yet visible to this sweep is seconds-to-minutes old at
#: most (it is committed RUNNING before its workspace is provisioned), so a full
#: day is an enormous safety margin over the only false-positive that matters.
_ORPHAN_GRACE_S = 24 * 3600


def _scan_run_workspace_dirs(root: Path) -> dict[uuid.UUID, float]:
    """``{run_id: dir mtime}`` for every per-run workspace under ``root``.

    A non-directory, a non-UUID name (never a run workspace) and a dir that
    vanishes mid-scan are all skipped rather than raising."""
    found: dict[uuid.UUID, float] = {}
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            run_id = uuid.UUID(child.name)
        except ValueError:
            continue
        try:
            found[run_id] = child.stat().st_mtime
        except OSError:
            continue
    return found


async def reap_terminal_run_workspaces(
    session: AsyncSession,
    *,
    remover: Callable[[uuid.UUID | None, uuid.UUID], Awaitable[None]] | None = None,
    runs_root: Path | None = None,
    orphan_grace_s: float = _ORPHAN_GRACE_S,
) -> list[uuid.UUID]:
    """Reclaim the on-disk workspace (``var/runs/<run_id>``) of every run that is
    finished — the periodic sweep that BOUNDS ``var/runs`` to the live-run set.

    Two classes are reclaimed:

    * **terminal runs** — the run row exists and is shipped / failed / cancelled.
      The inline ship/discard/cancel hooks are not enough on their own: the FAILED
      transition has NO cleanup hook at all, and a crash between the terminal DB
      flip and any inline cleanup leaves the dir behind (a leak of 19GB / 161 dirs
      was found in production before this sweep existed).
    * **aged orphans** — the run row is GONE (hard-deleted with its product, or
      purged), so the terminal query can never match the dir. 49 such orphans
      (760MB) sat in production indefinitely. They are only reclaimed once older
      than ``orphan_grace_s``, because a brand-new run mid-provision is also
      row-less to this sweep — and it is seconds old, never a day.

    A dir for a NON-terminal run (its workspace is in use) and a non-UUID dir are
    always left alone. The remover is best-effort per dir: one failure is logged
    and the sweep continues.
    """
    from backend.config import get_settings  # noqa: PLC0415 — avoid import cycle

    root = runs_root or Path(get_settings().run_workspace_root)
    if not root.is_dir():
        return []

    now = datetime.now(tz=UTC).timestamp()
    on_disk = _scan_run_workspace_dirs(root)
    if not on_disk:
        return []

    known = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(ExecutionRun.id, ExecutionRun.product_id).where(ExecutionRun.id.in_(on_disk))
            )
        ).all()
    }
    terminal = {
        row[0]
        for row in (
            await session.execute(
                select(ExecutionRun.id).where(
                    ExecutionRun.id.in_(on_disk),
                    ExecutionRun.status.in_(tuple(_TERMINAL)),
                )
            )
        ).all()
    }

    #: (run_id, product_id) pairs to reclaim — terminal runs, plus orphans (no
    #: row) that have aged past the grace window. Everything else stays.
    targets: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    for run_id, mtime in on_disk.items():
        if run_id in terminal:
            targets.append((run_id, known.get(run_id)))
        elif run_id not in known and (now - mtime) >= orphan_grace_s:
            targets.append((run_id, None))
    if not targets:
        return []

    if remover is None:
        from backend.storage.product_workspace import (  # noqa: PLC0415
            remove_run_worktree,
        )

        remover = remove_run_worktree

    reaped: list[uuid.UUID] = []
    for run_id, product_id in targets:
        try:
            await remover(product_id, run_id)
            reaped.append(run_id)
        except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
            logger.warning(
                "reap_terminal_workspace_failed",
                run_id=str(run_id),
                exc_info=True,
            )
    if reaped:
        logger.info("reaped_terminal_run_workspaces", count=len(reaped))
    return reaped


async def reap_orphan_product_workspaces(
    session: AsyncSession,
    *,
    remover: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    products_root: Path | None = None,
    grace_s: float = _ORPHAN_GRACE_S,
) -> list[uuid.UUID]:
    """Reclaim ``var/products/<product_id>`` for every product whose row is GONE
    — the sweep that bounds ``var/products`` to the set of live products.

    The delete handler removes the repo inline, so this is the backstop for the
    products deleted before that existed (18 orphans holding 300MB — 90% of
    ``var/products`` — were found in production) and for any inline failure.

    Mirrors :func:`reap_terminal_run_workspaces`: a dir must be older than
    ``grace_s`` to be reaped. The repo is provisioned right after the row
    commits, so the race is already tiny, but the grace window removes it
    entirely at no cost. A non-UUID dir is never touched.
    """
    from backend.config import get_settings  # noqa: PLC0415 — avoid import cycle
    from backend.identity.workspaces_db import ProductRow  # noqa: PLC0415

    root = products_root or Path(get_settings().product_workspace_root)
    if not root.is_dir():
        return []

    now = datetime.now(tz=UTC).timestamp()
    on_disk = _scan_run_workspace_dirs(root)  # same shape: <uuid> dirs + mtimes
    if not on_disk:
        return []

    live = {
        row[0]
        for row in (
            await session.execute(select(ProductRow.id).where(ProductRow.id.in_(on_disk)))
        ).all()
    }
    targets = [
        pid for pid, mtime in on_disk.items() if pid not in live and (now - mtime) >= grace_s
    ]
    if not targets:
        return []

    if remover is None:
        from backend.storage.product_workspace import (  # noqa: PLC0415
            remove_product_workspace,
        )

        remover = remove_product_workspace

    reaped: list[uuid.UUID] = []
    for product_id in targets:
        try:
            await remover(product_id)
            reaped.append(product_id)
        except Exception:  # noqa: BLE001 — one bad dir must not abort the sweep
            logger.warning(
                "reap_orphan_product_workspace_failed",
                product_id=str(product_id),
                exc_info=True,
            )
    if reaped:
        logger.info("reaped_orphan_product_workspaces", count=len(reaped))
    return reaped


#: How long a product must go untouched before its on-disk repo is reclaimed.
#: The dial between disk footprint and R2 churn: reclaiming a product that was
#: just worked on only buys a re-download on the next run. ``0`` makes the
#: reclaim fire as soon as a product's last run goes terminal — full symmetry
#: with the github path, at the cost of re-fetching on every run.
_PRODUCT_IDLE_GRACE_S = 24 * 3600


async def reap_idle_product_workspaces(
    session: AsyncSession,
    *,
    publisher: Callable[[uuid.UUID], Awaitable[bool]] | None = None,
    remover: Callable[[uuid.UUID], Awaitable[None]] | None = None,
    products_root: Path | None = None,
    idle_grace_s: float | None = None,
) -> list[uuid.UUID]:
    """Reclaim the on-disk repo of products nobody is working on, so the disk
    holds only active work rather than every product that ever existed.

    The repo on disk is a CACHE; the bundle in the store is the record. Three
    gates, each closing a way this could destroy work:

    1. **A live run keeps its product.** A non-terminal run's worktree is linked
       to the product repo — deleting it mid-run breaks the run outright.
    2. **Recently-active products stay.** Reclaiming a product that was just
       worked on only buys a re-download on the next run.
    3. **Publish first, and only reclaim on a CLEAN publish.** This is what
       makes the best-effort publishes on the ship paths safe: if the newest
       state never reached the store — an outage, or an unresolved merge
       conflict with the store's copy — the repo STAYS and the product simply
       keeps costing disk until someone resolves it. Nothing is ever deleted on
       the strength of an assumption that it was published.

    Dirs with no product row are left to :func:`reap_orphan_product_workspaces`,
    which has its own longer grace and does not try to publish a dead product.
    """
    from backend.config import get_settings  # noqa: PLC0415 — avoid import cycle
    from backend.identity.workspaces_db import ProductRow  # noqa: PLC0415

    settings = get_settings()
    root = products_root or Path(settings.product_workspace_root)
    if not root.is_dir():
        return []
    on_disk = set(_scan_run_workspace_dirs(root))
    if not on_disk:
        return []

    if idle_grace_s is None:
        idle_grace_s = getattr(settings, "product_repo_idle_grace_s", _PRODUCT_IDLE_GRACE_S)
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=idle_grace_s)
    busy = {
        row[0]
        for row in (
            await session.execute(
                select(ExecutionRun.product_id).where(
                    ExecutionRun.product_id.in_(on_disk),
                    ExecutionRun.status.not_in(tuple(_TERMINAL)),
                )
            )
        ).all()
    }
    # Activity = the product row's own updated_at OR any run's, whichever is
    # newer: a product created moments ago has no runs yet but is very much live.
    recent = {
        row[0]
        for row in (
            await session.execute(
                select(ExecutionRun.product_id).where(
                    ExecutionRun.product_id.in_(on_disk),
                    ExecutionRun.updated_at >= cutoff,
                )
            )
        ).all()
    }
    candidates = [
        row[0]
        for row in (
            await session.execute(
                select(ProductRow.id).where(
                    ProductRow.id.in_(on_disk),
                    ProductRow.updated_at < cutoff,
                )
            )
        ).all()
        if row[0] not in busy and row[0] not in recent
    ]
    if not candidates:
        return []

    if publisher is None:
        publisher = _bundle_publisher_for(session)
    if remover is None:
        from backend.storage.product_workspace import (  # noqa: PLC0415
            remove_product_workspace,
        )

        remover = remove_product_workspace

    reaped: list[uuid.UUID] = []
    for product_id in candidates:
        try:
            if not await publisher(product_id):
                logger.info(
                    "product_repo_kept_unpublished",
                    product_id=str(product_id),
                )
                continue
            await remover(product_id)
            reaped.append(product_id)
        except Exception:  # noqa: BLE001 — one product must not abort the sweep
            logger.warning(
                "reap_idle_product_workspace_failed",
                product_id=str(product_id),
                exc_info=True,
            )
    if reaped:
        logger.info("reaped_idle_product_workspaces", count=len(reaped))
    return reaped


def _bundle_publisher_for(
    session: AsyncSession,
) -> Callable[[uuid.UUID], Awaitable[bool]]:
    """The default publisher: publish under the product lock and report whether
    the store now holds the product's newest state — the ONLY condition under
    which the local repo may be deleted.

    The lock is taken with the reaper's own session, so a ship in flight for
    this product makes the reclaim back off rather than race its merge.
    """

    async def _publish(product_id: uuid.UUID) -> bool:
        from backend.storage.product_workspace import (  # noqa: PLC0415
            ProductWorkspaceBusy,
            product_workspace_lock,
            publish_product_bundle,
        )

        try:
            async with product_workspace_lock(session, product_id):
                outcome = await publish_product_bundle(product_id)
        except ProductWorkspaceBusy:
            return False
        return outcome.published and outcome.status == "clean"

    return _publish


async def _cancel(session: AsyncSession, run: ExecutionRun, *, reason: str) -> bool:
    """Flip a run to CANCELLED + append the audit-history row. Returns ``False``
    if the run is already terminal (no-op), mirroring ``AgentRunner.transition``."""
    if run.status in _TERMINAL:
        return False
    from_status = run.status
    run.status = RunStatus.CANCELLED
    run.updated_at = datetime.now(tz=UTC)
    session.add(
        ExecutionRunHistory(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            from_status=from_status,
            to_status=RunStatus.CANCELLED,
            reason=reason,
            created_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()
    logger.info(
        "run_cancelled",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
        from_status=from_status.value,
        reason=reason,
    )
    return True


async def _reopen(session: AsyncSession, run: ExecutionRun, *, reason: str) -> None:
    """Flip a terminal-failed run back to OPEN + append the audit-history row.

    The re-open counterpart of :func:`_cancel`, and the one exit
    :meth:`AgentRunner.transition` allows out of CANCELLED.
    """
    from_status = run.status
    run.status = RunStatus.OPEN
    run.updated_at = datetime.now(tz=UTC)
    session.add(
        ExecutionRunHistory(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            from_status=from_status,
            to_status=RunStatus.OPEN,
            reason=reason,
            created_at=datetime.now(tz=UTC),
        )
    )
    await session.flush()
    logger.info(
        "run_reopened",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
        from_status=from_status.value,
        reason=reason,
    )


async def _resolve_pending_decisions(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    reason: str,
    actor_id: uuid.UUID | None,
) -> list[str]:
    """Resolve a run's PENDING decisions so they drop off the Summary dashboard.

    The Summary "확인 필요" surface lists PENDING :class:`Decision` rows
    (``GET /api/v1/checkpoints`` → ``list_pending_by_workspace``), NOT run
    status — so cancelling the run alone leaves its "답변이 필요해요" card up.
    Mirrors the fields the ``/checkpoints/{id}/resolve`` handler writes.
    """
    decisions = SqlAlchemyDecisionRepository(session)
    now = datetime.now(tz=UTC)
    resolved: list[str] = []
    for dec in await decisions.list_by_run(run.id, run.workspace_id):
        if dec.status is not DecisionStatus.PENDING:
            continue
        dec.status = DecisionStatus.RESOLVED
        dec.resolution = reason
        dec.resolved_at = now
        dec.resolved_by = actor_id
        resolved.append(str(dec.id))
    if resolved:
        await session.flush()
    return resolved


async def _resolve_pending_safe_mode_items(session: AsyncSession, run: ExecutionRun) -> list[str]:
    """Deny a run's PENDING safe-mode approval items.

    A cancelled run's deliverables will never be delivered, so their approval
    cards must drop off the Decisions queue — the same orphaned-half fix as
    :func:`_resolve_pending_decisions`, for the ``safe_mode_queue_items`` surface
    (``GET /api/v1/checkpoints`` → ``list_pending_by_workspace``). Terminal deny,
    no downstream dispatch (an un-delivered pending item has nothing to undo)."""
    queue = SqlAlchemySafeModeQueueRepository(session)
    now = datetime.now(tz=UTC)
    resolved: list[str] = []
    for item in await queue.list_pending_for_run(workspace_id=run.workspace_id, run_id=run.id):
        item.status = SafeModeStatus.DENIED
        item.decided_at = now
        resolved.append(str(item.id))
    if resolved:
        await session.flush()
    return resolved


@dataclass
class CancelOutcome:
    """Result of :func:`cancel_run`."""

    found: bool
    cancelled: bool
    status: str | None  # final run status value, or None when not found
    decisions_resolved: list[str] = field(default_factory=list)
    safe_mode_items_resolved: list[str] = field(default_factory=list)


@dataclass
class DiscardOutcome:
    """Result of :func:`discard_run`."""

    run_id: uuid.UUID
    status: str
    cancelled: bool
    deliverables_retracted: list[str] = field(default_factory=list)
    deliverables_need_compensation: list[str] = field(default_factory=list)
    decisions_resolved: list[str] = field(default_factory=list)
    safe_mode_items_resolved: list[str] = field(default_factory=list)


@dataclass
class RetryOutcome:
    """Result of :func:`retry_run`."""

    found: bool
    retried: bool
    status: str | None  # run status value after the call, or None when not found
    retry_count: int = 0


# Only a terminal-FAILED run can be retried. A paused (RUNNING, needs-decision)
# run is resolved via the Decision's ``retry`` action instead.
_RETRYABLE: frozenset[RunStatus] = frozenset({RunStatus.FAILED, RunStatus.CANCELLED})


async def retry_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> RetryOutcome:
    """Re-open a FAILED / CANCELLED run for another attempt (L2 / #9).

    The single rule behind ``POST /runs/{id}/retry`` and ``bsvibe_runs_retry``.
    Runs are still never *created* through either surface — retry is the one
    founder-initiated mutation on an existing one: a failed run is recoverable,
    not a dead end.

    ``found=False`` for an unknown / cross-workspace id (existence is never
    leaked across the boundary); ``retried=False`` with the current status for a
    run that is not terminal-failed. Like :func:`cancel_run`, the caller owns
    the commit — both surfaces commit after mapping the outcome.
    """
    runs = SqlAlchemyRunRepository(session)
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        return RetryOutcome(found=False, retried=False, status=None)
    if run.status not in _RETRYABLE:
        return RetryOutcome(found=True, retried=False, status=run.status.value)

    # Bump a retry marker on the free-form payload (re-assign, not in-place
    # mutate, so SQLAlchemy detects the JSON-column change). drive_once preserves
    # it (it spreads the existing payload), so observability + future loop logic
    # can see this is a re-attempt.
    payload: dict[str, Any] = dict(run.payload or {})
    retry_count = int(payload.get("retry_count", 0)) + 1
    payload["retry_count"] = retry_count
    # L9 — reset the elapsed-time clock: the review surfaces count from
    # ``restarted_at`` (when present) instead of ``created_at`` so a retried run
    # shows time since THIS attempt began, not since the first start.
    payload["restarted_at"] = datetime.now(tz=UTC).isoformat()
    run.payload = payload
    await session.flush()

    # FAILED / CANCELLED → OPEN so AgentWorker.drive_once (scans OPEN runs)
    # re-picks it for a fresh attempt. The history row records the re-open.
    #
    # Inlined like :func:`_cancel` rather than calling ``AgentRunner.transition``
    # — see the module docstring: this service is reachable from the MCP leaf
    # surface and must not drag the agent engine into its import graph. OPEN is
    # safe to inline because ``transition``'s side effects are all keyed on
    # other targets (the FAILED notification funnel, the REVIEW_READY auto-ship
    # + step handoff); none of them fire for OPEN.
    await _reopen(session, run, reason=f"founder retry (attempt {retry_count + 1})")
    return RetryOutcome(
        found=True, retried=True, status=RunStatus.OPEN.value, retry_count=retry_count
    )


async def cancel_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reason: str,
    actor_id: uuid.UUID | None = None,
) -> CancelOutcome:
    """Cancel an OPEN / RUNNING run (mirrors ``POST /runs/{id}/cancel``).

    ``found=False`` for an unknown / cross-workspace id; ``cancelled=False`` with
    the current status for a run that is not in-flight (terminal or review_ready
    — use :func:`discard_run` for the latter).

    Also resolves the run's PENDING decisions (like :func:`discard_run` and
    :func:`cancel_product_runs`) — cancelling the run alone leaves its Summary
    "확인 필요" card up forever (orphaned-half).
    """
    runs = SqlAlchemyRunRepository(session)
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        return CancelOutcome(found=False, cancelled=False, status=None)
    if run.status not in _CANCELLABLE:
        return CancelOutcome(found=True, cancelled=False, status=run.status.value)
    await _cancel(session, run, reason=reason)
    resolved = await _resolve_pending_decisions(session, run, reason=reason, actor_id=actor_id)
    sm_resolved = await _resolve_pending_safe_mode_items(session, run)
    # Cancel leaves the run's worktree on disk (unlike discard, which removes it
    # entirely). If the run was cancelled while a verify-time ``merge main`` was
    # mid-flight, the worktree carries ``<<<<<<<`` markers + MERGE_HEAD — abort
    # so nothing committable lingers. Best-effort no-op when no merge is running.
    await _abort_merge_best_effort(run)
    return CancelOutcome(
        found=True,
        cancelled=True,
        status=RunStatus.CANCELLED.value,
        decisions_resolved=resolved,
        safe_mode_items_resolved=sm_resolved,
    )


async def discard_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reason: str,
    actor_id: uuid.UUID | None = None,
) -> DiscardOutcome | None:
    """Discard a run — cancel it (if non-terminal) + best-effort tombstone.

    Returns ``None`` for an unknown / cross-workspace id. Transitions a
    non-terminal run → CANCELLED (a terminal run is left as-is but its deliverables
    are still evaluated). Handle-less deliverables are tombstoned; deliverables
    with compensation handles are surfaced for an explicit compensating retract.
    Worktree removal is best-effort and never fails the discard.
    """
    runs = SqlAlchemyRunRepository(session)
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        return None

    cancelled = await _cancel(session, run, reason=reason)
    decisions_resolved = await _resolve_pending_decisions(
        session, run, reason=reason, actor_id=actor_id
    )
    safe_mode_resolved = await _resolve_pending_safe_mode_items(session, run)

    deliverables = SqlAlchemyDeliverableRepository(session)
    now = datetime.now(tz=UTC)
    retracted: list[str] = []
    need_compensation: list[str] = []
    for d in await deliverables.list_by_run(run.id, workspace_id):
        if d.retracted_at is not None:
            continue
        if d.compensation_handles:
            # A delivered external artifact — don't fake a rollback.
            need_compensation.append(str(d.id))
            continue
        d.retracted_at = now
        retracted.append(str(d.id))

    await session.flush()
    await _remove_worktree_best_effort(run)

    logger.info(
        "run_discarded",
        run_id=str(run.id),
        workspace_id=str(workspace_id),
        cancelled=cancelled,
        deliverables_retracted=len(retracted),
        deliverables_need_compensation=len(need_compensation),
        decisions_resolved=len(decisions_resolved),
        safe_mode_items_resolved=len(safe_mode_resolved),
    )
    return DiscardOutcome(
        run_id=run.id,
        status=run.status.value,
        cancelled=cancelled,
        deliverables_retracted=retracted,
        deliverables_need_compensation=need_compensation,
        decisions_resolved=decisions_resolved,
        safe_mode_items_resolved=safe_mode_resolved,
    )


async def cancel_product_runs(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    workspace_id: uuid.UUID,
    reason: str,
    actor_id: uuid.UUID | None = None,
) -> int:
    """Cancel every non-terminal run bound to a product; return the count.

    Called from ``delete_product`` before the product row is deleted so its runs
    (loose ``product_id`` reference, no FK cascade) are never orphaned.
    """
    stmt = select(ExecutionRun).where(
        ExecutionRun.workspace_id == workspace_id,
        ExecutionRun.product_id == product_id,
        ExecutionRun.status.not_in(tuple(_TERMINAL)),
    )
    rows = (await session.execute(stmt)).scalars().all()
    cancelled = 0
    for run in rows:
        if await _cancel(session, run, reason=reason):
            cancelled += 1
        await _resolve_pending_decisions(session, run, reason=reason, actor_id=actor_id)
        await _resolve_pending_safe_mode_items(session, run)
    return cancelled


async def _remove_worktree_best_effort(run: ExecutionRun) -> None:
    """Remove a run's git worktree without ever failing the caller.

    ``git worktree remove --force`` deletes the whole worktree directory (and
    the branch), so any mid-merge markers vanish with it — ``discard`` therefore
    needs no separate :func:`abort_merge` (that is why only ``cancel``, which
    keeps the worktree, calls :func:`_abort_merge_best_effort`)."""
    if run.product_id is None:
        return
    try:
        from backend.storage.product_workspace import remove_run_worktree  # noqa: PLC0415

        await remove_run_worktree(run.product_id, run.id)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning(
            "run_discard_worktree_cleanup_failed",
            run_id=str(run.id),
            product_id=str(run.product_id),
            exc_info=True,
        )


async def _abort_merge_best_effort(run: ExecutionRun) -> None:
    """``git merge --abort`` a run's worktree without ever failing the caller.

    Used by ``cancel`` (which leaves the worktree on disk) so a run cancelled
    mid-merge doesn't leave committable ``<<<<<<<`` markers behind. Idempotent:
    :func:`abort_merge` is a no-op when no merge is in progress."""
    if run.product_id is None:
        return
    try:
        from backend.storage.product_workspace import abort_merge  # noqa: PLC0415

        await abort_merge(run.product_id, run.id)
    except Exception:  # noqa: BLE001 — cleanup is best-effort
        logger.warning(
            "run_cancel_abort_merge_failed",
            run_id=str(run.id),
            product_id=str(run.product_id),
            exc_info=True,
        )


__all__ = [
    "CancelOutcome",
    "DiscardOutcome",
    "RetryOutcome",
    "cancel_product_runs",
    "cancel_run",
    "discard_run",
    "retry_run",
]
