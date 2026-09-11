"""A founder's chat answer, queued for the engine to apply — 게이트 3 후속.

Answering a ``needs_you`` Decision is heavy: ``resolve_checkpoint`` records the
answer, flips the run ``RUNNING → OPEN`` so a worker re-drives it, and for
``ship`` / ``discard`` runs side-effecting handlers. Two reasons that cannot
happen in the inbound request:

1. **The contract.** ``resolve_checkpoint`` transitively imports
   ``plugin.audit``, and the R2c gate forbids the engine inbound layer reaching
   ``plugin`` — its comment naming this exact move: *"if they ever do, this gate
   surfaces it as the reverse-direction violation it is."*
2. **The shape of the product.** A chat MESSAGE — the heavier direction, since it
   creates a run — already works this way: the route lands a ``TriggerEvent``,
   returns 202, and the IntakeWorker builds the run. **The inbound layer records;
   the engine decides.** A tap that resolved inline would be the one place that
   broke that rule, inside a callback Telegram wants answered fast.

So the tap validates (workspace scope, still pending, option in range), writes
the choice, and returns. :mod:`backend.workflow.application.decision_answer_drain`
applies it.

**Why the Decision's own payload and not a new table.** It is the row the answer
answers, so there is nothing to join and nothing to garbage-collect; clearing the
key on success makes the apply idempotent; and it needs no migration. It mirrors
``audit_outbox``'s contract — *"lands a row inside the request transaction, the
worker drains it on its own schedule"* — at the size one call deserves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: Reserved key on ``Decision.payload``. Namespaced so it can never collide with
#: the work LLM's own keys (``question`` / ``options`` / ``reason``).
QUEUED_ANSWER_KEY = "_queued_chat_answer"


@dataclass(frozen=True)
class QueuedAnswer:
    """One founder answer waiting for the engine.

    Exactly one of ``action_key`` / ``answer`` is meaningful, mirroring
    ``resolve_checkpoint``'s own two ways to be answered: a one-click action, or
    the text of a chosen option.
    """

    action_key: str | None
    answer: str
    actor_id: uuid.UUID
    connector: str


def queued_answer(decision: Any) -> QueuedAnswer | None:
    """The answer waiting on ``decision``, or ``None``.

    Tolerant by construction: a half-written or hand-edited payload yields
    ``None`` rather than raising, because this is read on the drain path where a
    bad row must not stall every other Decision behind it.
    """
    raw = (decision.payload or {}).get(QUEUED_ANSWER_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        actor_id = uuid.UUID(str(raw["actor_id"]))
    except (KeyError, ValueError, TypeError):
        logger.warning("queued_answer_unreadable", decision_id=str(getattr(decision, "id", "")))
        return None
    action_key = raw.get("action_key")
    return QueuedAnswer(
        action_key=str(action_key) if action_key else None,
        answer=str(raw.get("answer") or ""),
        actor_id=actor_id,
        connector=str(raw.get("connector") or ""),
    )


def queue_answer(
    decision: Any,
    *,
    action_key: str | None,
    answer: str,
    actor_id: uuid.UUID,
    connector: str,
) -> bool:
    """Record the founder's choice on ``decision``. ``False`` if one is already
    queued.

    First tap wins. The card stays on the founder's phone after the tap, so a
    second press is expected traffic — and overwriting would race the drain,
    which may already be applying the first answer.

    Mutates in memory only; the caller's transaction makes it durable.
    """
    payload = dict(decision.payload or {})
    if isinstance(payload.get(QUEUED_ANSWER_KEY), dict):
        return False
    payload[QUEUED_ANSWER_KEY] = {
        "action_key": action_key,
        "answer": answer,
        "actor_id": str(actor_id),
        "connector": connector,
        "queued_at": datetime.now(tz=UTC).isoformat(),
    }
    # Reassign rather than mutate: ``payload`` is a JSON column and SQLAlchemy
    # does not see in-place edits to a plain dict as dirty.
    decision.payload = payload
    return True


def clear_queued_answer(decision: Any) -> None:
    """Drop the queued answer — the engine applied it (or gave up on it).

    Clearing is what makes the drain idempotent: a redelivered or retried apply
    finds nothing to do instead of resolving the Decision twice.
    """
    payload = dict(decision.payload or {})
    payload.pop(QUEUED_ANSWER_KEY, None)
    decision.payload = payload


__all__ = [
    "QUEUED_ANSWER_KEY",
    "QueuedAnswer",
    "clear_queued_answer",
    "queue_answer",
    "queued_answer",
]
