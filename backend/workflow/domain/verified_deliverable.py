"""Shared verified-terminal artifact writes — ONE source of truth.

Workflow §1 (verified terminal) / §11.3. When a run reaches ``verified`` the
backend must land a stable, well-known artifact contract regardless of HOW the
work was produced:

* the native agent loop (:class:`~backend.execution.orchestrator.RunOrchestrator`),
* or an external CLI worker (:class:`~backend.executors.orchestrator.ExecutorOrchestrator`,
  Lift 5b of the executor-pool epic).

Both paths call :func:`write_verified_deliverable` so the
``Deliverable`` / ``DeliveryEventRow`` / settle-activity shape has a single
definition — diverging the artifact shape across compute backends would silently
break every downstream consumer (DeliveryWorker, SettleWorker, the PWA Brief).

This helper is the *write* contract only — it does NOT transition the WorkStep /
RunAttempt rows nor emit the Redis wake-up notifications (those stay
caller-owned, since each compute path owns its own attempt bookkeeping and the
native loop's stream emission is gated on its own redis client). It ``add``s +
``flush``es but never ``commit``s (the caller owns the transaction boundary).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    ExecutionRunActivity,
)

logger = structlog.get_logger(__name__)

_SETTLE_SUMMARY_CAP = 500

# Cap for the captured unified diff stored on the deliverable payload. A typical
# deliverable diff is a few KB; this guards a runaway diff (a large generated/
# vendored file) from bloating the row. Past it, the leading bytes are kept and
# ``diff_truncated`` is flagged so the viewer shows a calm "showing the first
# part" note rather than persisting an unbounded blob.
_MAX_DIFF_CHARS = 256 * 1024


async def _capture_product_run_diff(run: ExecutionRun) -> tuple[str | None, bool]:
    """The run's own changes as a (possibly truncated) unified diff, or ``(None,
    False)``. Product runs only — a non-product (Direct) run has no worktree and
    no 'before' state, so there is nothing to diff. Best-effort: any failure
    degrades to no diff, never breaks the verified terminal."""
    if run.product_id is None:
        return None, False
    from backend.storage.product_workspace import (
        capture_run_diff,  # noqa: PLC0415 — lazy, cross-layer
    )

    diff = await capture_run_diff(run.product_id, run.id)
    if diff is None:
        return None, False
    if len(diff) > _MAX_DIFF_CHARS:
        return diff[:_MAX_DIFF_CHARS], True
    return diff, False


async def settle_run_context(session: AsyncSession, run: ExecutionRun) -> dict[str, Any]:
    """Resolve the run's stable settle-clustering context.

    ``intent_text`` is the founder's own Direction (set by intake); the product
    slug/name is the run's product binding (resolved from ``run.product_id``).
    Both are stable inputs the SettleWorker uses as canonicalization cluster
    keys — never the work LLM's / worker's free output. Only present keys are
    returned (a connector-inbound run with no product/intent yields ``{}``).
    Resolution is best-effort — a missing/deleted product row is omitted, never
    an exception that could break the verified terminal.
    """
    context: dict[str, Any] = {}
    intent_text = (run.payload or {}).get("intent_text")
    if isinstance(intent_text, str) and intent_text.strip():
        context["intent_text"] = intent_text
    if run.product_id is not None:
        from backend.identity.workspaces_db import ProductRow  # noqa: PLC0415 — cross-domain, local

        product = await session.get(ProductRow, run.product_id)
        if product is not None:
            context["product_slug"] = product.slug
            context["product_name"] = product.name
    return context


async def write_verified_deliverable(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    attempt_id: uuid.UUID,
    artifact_refs: list[str],
    summary: str,
    knowledge: RememberableKnowledge | None = None,
) -> Deliverable:
    """Write the verified-terminal artifacts for ``run`` and return the Deliverable.

    Emits exactly what the native loop's ``_finish_verified`` has always written:

    1. a CODE :class:`Deliverable` (``payload={"artifact_refs", "summary"}``),
    2. a :class:`DeliveryEventRow` (drained by the DeliveryWorker), and
    3. a ``settle`` :class:`ExecutionRunActivity` carrying the run's stable
       clustering context (intent/product) + ``verified: True``.

    The summary is truncated to :data:`_SETTLE_SUMMARY_CAP` chars in the deliver
    event + settle payloads (matching the native path); the Deliverable itself
    keeps the full summary.
    """
    # Lift 2a: capture the run's real old↔new diff while the worktree is still
    # alive (verify-time, before auto-ship cleanup) so the report can render
    # GitHub-style red/green. Best-effort + product-run only; a missing diff
    # leaves the payload as before and the viewer falls back to additions.
    payload: dict[str, Any] = {"artifact_refs": artifact_refs, "summary": summary}
    diff, diff_truncated = await _capture_product_run_diff(run)
    if diff is not None:
        payload["diff"] = diff
        if diff_truncated:
            payload["diff_truncated"] = True

    deliverable = Deliverable(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        deliverable_type=DeliverableType.CODE,
        artifact_uri=None,
        diff_url=None,
        payload=payload,
    )
    session.add(deliverable)
    await session.flush()

    # Deliver event — drained by the DeliveryWorker (delivery_events table).
    # ``run_id`` is the per-Run grouping key (B12a / Workflow §1.2 — Safe Mode
    # as transactional container): the DeliveryWorker threads it onto the
    # SafeModeQueueItemRow so the founder can approve every queued item of a
    # run together.
    from backend.workflow.infrastructure.delivery.db import (
        DeliveryEventRow,  # noqa: PLC0415 — cross-domain, local
    )

    session.add(
        DeliveryEventRow(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            deliverable_id=deliverable.id,
            artifact_type=DeliverableType.CODE.value,
            payload={"artifact_refs": artifact_refs, "summary": summary[:_SETTLE_SUMMARY_CAP]},
        )
    )

    # Settle observation — the run-trace/observation side channel (§1).
    settle_payload: dict[str, Any] = {
        "attempt_id": str(attempt_id),
        "verified": True,
        "artifact_refs": artifact_refs,
        "summary": summary[:_SETTLE_SUMMARY_CAP],
        **await settle_run_context(session, run),
    }
    # v2 — the knowledge the WORKING agent declared (retrospective-style). Rides
    # the settle payload so the SettleWorker's sink writes a topic-titled note
    # authored by the agent with full working context. Absent for routine work
    # (the agent declared none) — there is no post-hoc extractor.
    if knowledge is not None:
        settle_payload["agent_knowledge"] = {
            "topic": knowledge.topic,
            "insight": knowledge.insight,
        }
    session.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type="settle",
            payload=settle_payload,
        )
    )
    await session.flush()
    logger.info(
        "verified_deliverable_written",
        run_id=str(run.id),
        deliverable_id=str(deliverable.id),
        artifact_refs=artifact_refs,
    )
    return deliverable


#: B12a — Marks a Deliverable's payload as a MID-LOOP partial Deliver event
#: (one external artifact the agent loop produced before terminating). Downstream
#: surfaces key off ``payload["kind"]`` to render it as a partial (e.g. a PR
#: opened mid-run) instead of a verified terminal artifact.
PARTIAL_DELIVERABLE_KIND = "mid_loop_partial"

_DELIVERABLE_TYPE_VALUES = {member.value for member in DeliverableType}


def _resolve_deliverable_type(raw: str) -> DeliverableType:
    """Map a free-form ``artifact_type`` string onto a :class:`DeliverableType`.

    The work LLM may emit any of the spec's artifact_type strings (``pr``,
    ``page``, ``issue_comment``, ``notion_page``, …). Known values mirror the
    enum 1:1; anything unknown is parked under ``DIRECT_OUTPUT`` so the row
    still persists (the original artifact_type lives on in payload + the
    DeliveryEventRow.artifact_type column, which is free-form).
    """
    if raw in _DELIVERABLE_TYPE_VALUES:
        return DeliverableType(raw)
    return DeliverableType.DIRECT_OUTPUT


async def write_partial_deliverable(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    artifact_type: str,
    summary: str,
    external_ref: str | None = None,
    channel: str | None = None,
) -> Deliverable | None:
    """Write a mid-loop partial Deliver event (B12a / Workflow §1).

    Each successful ``emit_deliverable`` tool call by the work LLM produces one
    of these — one external artifact (PR, page, comment, draft, …) the loop
    emitted BEFORE reaching the verified terminal. The DeliveryWorker drains
    the resulting DeliveryEventRow exactly like the terminal write (so Safe
    Mode gating + dispatch are uniform).

    Idempotent on ``external_ref``: when the same ``(run_id, external_ref)``
    has already been emitted this run, returns ``None`` (no row written, no
    DeliveryEventRow either) — the LLM occasionally re-emits the same artifact
    and we don't want duplicate Safe Mode queue items.

    No settle activity here — settle is the canonical-knowledge side channel
    (Workflow §1) and a mid-loop partial isn't a settled observation. Settle
    still fires once at the verified terminal via
    :func:`write_verified_deliverable`.
    """
    if external_ref:
        # Idempotency on ``(run_id, external_ref)`` — done Python-side rather
        # than via a JSON path operator so SQLite tests + PG prod behave
        # identically. The per-run partial set is tiny (single-digit emits per
        # run in practice), so loading them once per dedupe check is cheap.
        existing_stmt = select(Deliverable).where(Deliverable.run_id == run.id)
        existing_rows = (await session.execute(existing_stmt)).scalars().all()
        for prior in existing_rows:
            prior_payload = prior.payload if isinstance(prior.payload, dict) else {}
            if prior_payload.get("external_ref") == external_ref:
                logger.info(
                    "partial_deliverable_deduped",
                    run_id=str(run.id),
                    external_ref=external_ref,
                )
                return None

    deliverable_type = _resolve_deliverable_type(artifact_type)
    payload: dict[str, Any] = {
        "kind": PARTIAL_DELIVERABLE_KIND,
        "artifact_type": artifact_type,
        "summary": summary[:_SETTLE_SUMMARY_CAP],
    }
    if external_ref:
        payload["external_ref"] = external_ref
    if channel:
        payload["channel"] = channel

    deliverable = Deliverable(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        deliverable_type=deliverable_type,
        artifact_uri=None,
        diff_url=None,
        payload=payload,
    )
    session.add(deliverable)
    await session.flush()

    from backend.workflow.infrastructure.delivery.db import (
        DeliveryEventRow,  # noqa: PLC0415 — cross-domain, local
    )

    session.add(
        DeliveryEventRow(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            deliverable_id=deliverable.id,
            artifact_type=artifact_type,
            payload=dict(payload),
        )
    )
    await session.flush()
    logger.info(
        "partial_deliverable_written",
        run_id=str(run.id),
        deliverable_id=str(deliverable.id),
        artifact_type=artifact_type,
        external_ref=external_ref,
    )
    return deliverable


#: Marks a Deliverable's payload as a knowledge-only ANSWER (B9b) — a concise
#: answer the founder reads, composed in ONE LLM call from BSage knowledge. It is
#: deliberately NOT verified code: ``write_answer_deliverable`` writes a
#: ``DIRECT_OUTPUT`` Deliverable (never CODE), sets no ProofState.PROVED, and runs
#: no VerificationResult. Downstream surfaces key off ``payload["kind"]`` to
#: render it as an answer, not a green "verified" code change (B4 trust integrity).
ANSWER_DELIVERABLE_KIND = "knowledge_answer"


async def write_answer_deliverable(
    session: AsyncSession,
    run: ExecutionRun,
    *,
    attempt_id: uuid.UUID,
    answer: str,
    knowledge_refs: list[str],
) -> Deliverable:
    """Write the HONEST terminal artifacts for a knowledge-only answer (B9b).

    A knowledge-only ask is answered DIRECTLY from BSage knowledge with one LLM
    call — it is NOT verified code, so this does NOT mark anything PROVED and does
    NOT write a CODE Deliverable. It emits:

    1. a :data:`DeliverableType.DIRECT_OUTPUT` :class:`Deliverable` whose payload
       carries the ``answer`` text + the ``knowledge_refs`` it was grounded in +
       ``kind = ANSWER_DELIVERABLE_KIND`` (so a consumer renders an answer, never
       a green verified-code change),
    2. a :class:`DeliveryEventRow` (drained by the DeliveryWorker — the answer is
       delivered like any other ``direct_output``), and
    3. a ``settle`` :class:`ExecutionRunActivity` carrying the run's stable
       clustering context (intent/product) + ``verified: False`` (NEVER True — the
       answer was not verified-as-code).

    ``add`` + ``flush`` only; the caller owns the transaction boundary (mirrors
    :func:`write_verified_deliverable`)."""
    deliverable = Deliverable(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=run.workspace_id,
        deliverable_type=DeliverableType.DIRECT_OUTPUT,
        artifact_uri=None,
        diff_url=None,
        payload={
            "kind": ANSWER_DELIVERABLE_KIND,
            "answer": answer,
            "knowledge_refs": knowledge_refs,
        },
    )
    session.add(deliverable)
    await session.flush()

    from backend.workflow.infrastructure.delivery.db import (
        DeliveryEventRow,  # noqa: PLC0415 — cross-domain, local
    )

    session.add(
        DeliveryEventRow(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            run_id=run.id,
            deliverable_id=deliverable.id,
            artifact_type=DeliverableType.DIRECT_OUTPUT.value,
            payload={
                "kind": ANSWER_DELIVERABLE_KIND,
                "answer": answer[:_SETTLE_SUMMARY_CAP],
            },
        )
    )

    settle_payload: dict[str, Any] = {
        "attempt_id": str(attempt_id),
        # NOT verified-as-code — a knowledge answer is an honest, unverified
        # founder-facing output (B4 trust integrity).
        "verified": False,
        "kind": ANSWER_DELIVERABLE_KIND,
        "answer": answer[:_SETTLE_SUMMARY_CAP],
        **await settle_run_context(session, run),
    }
    session.add(
        ExecutionRunActivity(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            activity_type="settle",
            payload=settle_payload,
        )
    )
    await session.flush()
    logger.info(
        "answer_deliverable_written",
        run_id=str(run.id),
        deliverable_id=str(deliverable.id),
        knowledge_refs=knowledge_refs,
    )
    return deliverable


__all__ = [
    "ANSWER_DELIVERABLE_KIND",
    "PARTIAL_DELIVERABLE_KIND",
    "settle_run_context",
    "write_answer_deliverable",
    "write_partial_deliverable",
    "write_verified_deliverable",
]
