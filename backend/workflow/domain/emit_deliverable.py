"""Loop emit_deliverable — mid-loop Deliver events (B12a / Workflow §1).

Lifted from ``backend.execution.orchestrator`` (Lift H2a / v8 §17.1).
The agent loop invokes :func:`handle_emit_deliverable` whenever the work
LLM calls the loop-owned ``emit_deliverable`` pseudo-tool. The helper
persists a partial :class:`Deliverable` (idempotent on ``external_ref``)
and publishes a tiny ``deliverable.partial`` LiveEvent so the PWA Brief
view wakes up AS the artifact lands.

Domain-layer module by design: persistence + bus publish are the *domain
semantics* of a mid-loop artifact emission. The ``LiveEventBus`` import
is local to avoid a cycle through ``backend.api.v1.live_events``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.db import ExecutionRun
from backend.execution.verified_deliverable import write_partial_deliverable

logger = structlog.get_logger(__name__)

EMIT_DELIVERABLE_NAME = "emit_deliverable"

EMIT_DELIVERABLE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": EMIT_DELIVERABLE_NAME,
        "description": (
            "Emit ONE external artifact you produced during this run — a partial "
            "Deliver event (B12a / Workflow §1). Use this whenever you have just "
            "produced one external thing (a PR, an issue comment, a Notion page, "
            "a draft) so the founder sees it on the Brief and Safe Mode can hold "
            "it for approval. Multi-artifact is the norm: emit ONE call per "
            "artifact (do NOT bundle several artifacts into one emit). This does "
            "NOT replace your verification — keep going through declare_verification "
            "+ tools + summary as usual; emit_deliverable is purely a side-channel "
            "for what already exists externally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_type": {
                    "type": "string",
                    "description": (
                        "What kind of artifact this is — e.g. 'pr', 'issue_comment', "
                        "'notion_page', 'page', 'page_image', 'direct_output'."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": ("A founder-readable one-liner — what you delivered and why."),
                },
                "external_ref": {
                    "type": "string",
                    "description": (
                        "Plugin-canonical id for the external artifact (used to "
                        "dedupe re-emits and as the compensation key). Example: "
                        "'github://acme/site/pull/15'. Optional but strongly "
                        "preferred — re-emitting the same external_ref is a no-op."
                    ),
                },
                "channel": {
                    "type": "string",
                    "description": (
                        "Where the artifact landed — 'github', 'notion', 'slack', etc. Optional."
                    ),
                },
            },
            "required": ["artifact_type", "summary"],
        },
    },
}


def _safe_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Truncate tool arguments for activity-payload logging (B12a).

    The agent-activity log carries each tool-call's args for replay/audit;
    long ``summary`` strings would balloon row sizes. Strings over 256 chars
    are truncated with an ellipsis."""
    capped: dict[str, Any] = {}
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > 256:
            capped[k] = v[:253] + "..."
        else:
            capped[k] = v
    return capped


async def handle_emit_deliverable(
    session: AsyncSession,
    run: ExecutionRun,
    arguments: dict[str, Any],
    *,
    live_event_bus: Any = None,
) -> str:
    """Persist a mid-loop Deliver event (B12a / Workflow §1).

    Validates the required ``artifact_type`` + ``summary`` strings, calls
    :func:`write_partial_deliverable`, and returns a JSON ack the LLM can
    read. Bad args produce a readable error tool result (the loop can
    recover); persistence failures degrade to an error string but never
    crash the loop.
    """
    artifact_type = str(arguments.get("artifact_type") or "").strip()
    summary = str(arguments.get("summary") or "").strip()
    external_ref_raw = arguments.get("external_ref")
    channel_raw = arguments.get("channel")
    external_ref = (
        str(external_ref_raw).strip()
        if isinstance(external_ref_raw, str) and external_ref_raw.strip()
        else None
    )
    channel = (
        str(channel_raw).strip() if isinstance(channel_raw, str) and channel_raw.strip() else None
    )
    if not artifact_type:
        return json.dumps(
            {
                "status": "error",
                "error": "emit_deliverable requires a non-empty 'artifact_type'.",
            }
        )
    if not summary:
        return json.dumps(
            {
                "status": "error",
                "error": "emit_deliverable requires a non-empty 'summary'.",
            }
        )
    try:
        deliverable = await write_partial_deliverable(
            session,
            run,
            artifact_type=artifact_type,
            summary=summary,
            external_ref=external_ref,
            channel=channel,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the loop
        logger.warning(
            "emit_deliverable_failed",
            run_id=str(run.id),
            artifact_type=artifact_type,
            error=str(exc),
        )
        return json.dumps({"status": "error", "error": str(exc)})
    if deliverable is None:
        return json.dumps(
            {
                "status": "deduped",
                "artifact_type": artifact_type,
                "external_ref": external_ref,
                "message": (
                    "This external_ref was already emitted earlier this run — "
                    "the second emit is a no-op (idempotent). Do not retry."
                ),
            }
        )
    # D6 — publish a tiny live-event so the PWA Run / Brief views wake up
    # AS the partial lands, not only at the verified terminal (Synthesis
    # §13 — Deliver as a continuous side channel). Soft-fail: a bus hiccup
    # must NEVER break the loop or revert the persisted Deliverable + the
    # DeliveryEventRow (those are the durable record; the bus is only the
    # wake-up signal — mirrors the audit→bridge pattern in
    # :mod:`plugin.audit.service`).
    await _publish_deliverable_partial_event(
        run=run,
        deliverable_id=deliverable.id,
        artifact_type=artifact_type,
        live_event_bus=live_event_bus,
    )
    return json.dumps(
        {
            "status": "emitted",
            "deliverable_id": str(deliverable.id),
            "artifact_type": artifact_type,
        }
    )


async def _publish_deliverable_partial_event(
    *,
    run: ExecutionRun,
    deliverable_id: uuid.UUID,
    artifact_type: str,
    live_event_bus: Any = None,
) -> None:
    """Fire one ``deliverable.partial`` LiveEvent for a mid-loop emit (D6).

    The payload is tiny — ids + the artifact_type label so the consumer can
    decide whether to refetch (B16 wire contract: no LLM content, just a
    wake-up). Uses the caller-supplied bus when set (tests), else the
    process-wide singleton (production — Redis transport bound at worker
    startup so the publish reaches the HTTP container's SSE subscribers).
    """
    try:
        from backend.api.v1.live_events import (  # noqa: PLC0415 — local, avoids cycle
            EVENT_DELIVERABLE_PARTIAL,
            LiveEvent,
            get_live_event_bus,
        )

        bus = live_event_bus or get_live_event_bus()
        await bus.publish(
            run.workspace_id,
            LiveEvent(
                event_type=EVENT_DELIVERABLE_PARTIAL,
                data={
                    "run_id": str(run.id),
                    "deliverable_id": str(deliverable_id),
                    "artifact_type": artifact_type,
                },
            ),
        )
    except BaseException:  # noqa: BLE001 — last-resort guard, never propagate
        logger.warning(
            "deliverable_partial_live_event_failed",
            run_id=str(run.id),
            deliverable_id=str(deliverable_id),
            exc_info=True,
        )


__all__ = [
    "EMIT_DELIVERABLE_NAME",
    "EMIT_DELIVERABLE_TOOL",
    "_publish_deliverable_partial_event",
    "_safe_args",
    "handle_emit_deliverable",
]
