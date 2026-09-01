"""The run-detail read model — derivation shared by REST and MCP.

Reads an ``ExecutionRun`` and the rows around it (decisions, the latest
verification, final + partial deliverables, the activity timeline) into the
:mod:`backend.workflow.serialization.run_views` shapes. Strictly read-only —
every write happens inside the agent loop.

Lives in the Workflow context, not under ``backend.api``, so the MCP
``bsvibe_runs_detail`` tool runs the SAME derivation the browser's run view
runs. Mirroring it into the MCP module would satisfy the parity guard while
creating exactly the drift that guard exists to catch.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.identity.workspaces_db import load_workspace_language
from backend.workflow.domain.repositories import (
    DecisionRepository,
    DeliverableRepository,
    RunRepository,
)
from backend.workflow.domain.verified_deliverable import PARTIAL_DELIVERABLE_KIND
from backend.workflow.infrastructure.db import (
    Decision,
    Deliverable,
    ExecutionRunActivity,
    ExecutionRunHistory,
    RunStatus,
    VerificationResult,
)
from backend.workflow.serialization.run_views import (
    RunActivity,
    RunDecision,
    RunDetailResponse,
    RunPartialDeliverable,
    RunTriggerContext,
    RunVerification,
)


def _opt_str(value: Any) -> str | None:
    """A non-empty string value, else ``None`` (tolerant of odd payload types)."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _intent_of(payload: Any) -> str | None:
    """The founder's Direction from a run's free-form payload (``intent_text``
    from intake, or ``text`` from a direct submission); ``None`` when neither is
    a non-empty string. Same resolution the trigger context + report use."""
    payload = payload if isinstance(payload, dict) else {}
    return _opt_str(payload.get("intent_text")) or _opt_str(payload.get("text"))


def _frame_field(payload: Any, key: str) -> str | None:
    """A non-empty string field out of the run's ``payload["frame"]`` block
    (L8 — ``summary_title`` / ``framed_intent``); ``None`` when absent or odd."""
    payload = payload if isinstance(payload, dict) else {}
    frame = payload.get("frame")
    if not isinstance(frame, dict):
        return None
    return _opt_str(frame.get(key))


def _restarted_at_of(payload: Any) -> datetime | None:
    """L9 — when the run was last restarted (founder retry), parsed from the
    ``restarted_at`` ISO string the retry endpoint stamps; ``None`` when absent
    or unparseable. The elapsed-time surface counts from here, not created_at."""
    payload = payload if isinstance(payload, dict) else {}
    raw = _opt_str(payload.get("restarted_at"))
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _trigger_context(payload: Any) -> RunTriggerContext:
    """Map the free-form run payload onto the trigger-context fields, defensively."""
    payload = payload if isinstance(payload, dict) else {}
    # The founder's Direction lives under ``intent_text`` (intake) or ``text``
    # (direct submission) — fall back across both.
    intent = _opt_str(payload.get("intent_text")) or _opt_str(payload.get("text"))
    return RunTriggerContext(
        source=_opt_str(payload.get("source")),
        trigger_kind=_opt_str(payload.get("trigger_kind")),
        intent_text=intent,
        product=_opt_str(payload.get("product")),
    )


def _question_text(decision: Decision) -> str:
    payload = decision.payload or {}
    if isinstance(payload, dict):
        value = payload.get("question")
        if isinstance(value, str):
            return value
    return ""


def _write_paths(payload: dict[str, Any]) -> list[str]:
    """The file paths a ``tool_call`` activity wrote, defensively (an odd
    ``writes`` value yields an empty list rather than throwing)."""
    writes = payload.get("writes")
    if not isinstance(writes, list):
        return []
    return [p for p in writes if isinstance(p, str) and p.strip()]


_VERIFY_LABELS = {
    "en": {
        "passed": "Verified the work",
        "failed": "Verification failed",
        "inconclusive": "Verification was inconclusive",
    },
    "ko": {
        "passed": "작업을 검증했어요",
        "failed": "검증에 실패했어요",
        "inconclusive": "검증이 판정 불가였어요",
    },
}

_VERIFY_FALLBACK = {"en": "Ran verification", "ko": "검증을 실행했어요"}

_STATIC_LABELS = {
    "settle": {"en": "Settled into knowledge", "ko": "지식으로 정리했어요"},
    "error": {"en": "Hit a problem", "ko": "문제가 생겼어요"},
}

_ROUTING_SOURCE_REASONS = {
    "en": {"explicit_rule": "your routing rule", "workspace_default": "workspace default"},
    "ko": {"explicit_rule": "라우팅 규칙", "workspace_default": "워크스페이스 기본값"},
}


def _lang(language: str) -> str:
    """The catalog key for ``language`` — anything we have no wording for reads
    English rather than a half-translated line."""
    return language if language in ("en", "ko") else "en"


def _tool_call_label(payload: dict[str, Any], language: str) -> str | None:
    """ "Delivered X" for a file-writing tool_call; ``None`` for a read-only one
    (noise the founder timeline skips)."""
    paths = _write_paths(payload)
    if not paths:
        return None
    shown = ", ".join(paths[:3])
    if len(paths) > 3:
        more = len(paths) - 3
        shown += f" 외 {more}개" if _lang(language) == "ko" else f" (+{more} more)"
    return f"{shown} 전달했어요" if _lang(language) == "ko" else f"Delivered {shown}"


def _routing_decision_label(payload: dict[str, Any], language: str) -> str | None:
    """ "Which model ran this step, and why" — the glass-box line.

    ``None`` for a payload we cannot state honestly (missing model, or a
    ``source`` we have no words for). A half-sentence explains nothing, and
    inventing a reason would be worse than staying silent.
    """
    target = payload.get("target")
    source = payload.get("source")
    if not isinstance(target, str) or not target:
        return None
    reasons = _ROUTING_SOURCE_REASONS[_lang(language)]
    reason = reasons.get(source) if isinstance(source, str) else None
    if reason is None:
        return None
    caller = payload.get("caller_id")
    ko = _lang(language) == "ko"
    if isinstance(caller, str) and caller:
        return (
            f"{caller} → {target} ({reason})" if ko else f"Routed {caller} to {target} ({reason})"
        )
    return f"모델 → {target} ({reason})" if ko else f"Routed to {target} ({reason})"


def _verify_label(payload: dict[str, Any], language: str) -> str | None:
    """The verifier's verdict in one calm phrase."""
    lang = _lang(language)
    outcome = payload.get("outcome")
    if isinstance(outcome, str):
        return _VERIFY_LABELS[lang].get(outcome, _VERIFY_FALLBACK[lang])
    return _VERIFY_FALLBACK[lang]


# Types whose label needs the payload, and types whose label is a constant.
# A dict rather than an if-chain so adding the next activity type is one line
# and never re-trips the return-count lint.
_LABEL_BUILDERS: dict[str, Callable[[dict[str, Any], str], str | None]] = {
    "tool_call": _tool_call_label,
    "verify": _verify_label,
    "routing_decision": _routing_decision_label,
}


def _activity_label(
    activity_type: str, payload: dict[str, Any], language: str = "en"
) -> str | None:
    """A short human label for one ExecutionRunActivity, or ``None`` when the
    event is low-signal noise the founder timeline should skip.

    Surfaced events tell the run's STORY: a file-writing ``tool_call``
    ("Delivered X"), a ``verify`` verdict, a ``settle``, a ``routing_decision``,
    and a calm ``error``. Per-turn ``llm_turn`` chatter and read-only
    ``tool_call`` rows are noise and drop out (→ ``None``). All payload reads are
    defensive so a malformed row degrades to a calm label / drop rather than
    500ing the response model.

    Localized at the PRODUCER (``workspaces.language``) — same discipline as the
    notification sentences. The heading was already translated; these lines were
    not, so a KO founder read Korean chrome over an English list.
    """
    builder = _LABEL_BUILDERS.get(activity_type)
    if builder is not None:
        return builder(payload, language)
    # llm_turn and any unknown / low-signal type are skipped.
    static = _STATIC_LABELS.get(activity_type)
    return static[_lang(language)] if static else None


def _partial_deliverable(row: Deliverable) -> RunPartialDeliverable:
    """D6 — map a mid-loop partial Deliverable row onto the response shape.

    All payload reads are defensive (a non-string ``summary``, missing
    ``artifact_type``, etc. degrade to ``None`` / the raw enum value) so a
    malformed payload never 500s the response model.
    """
    payload = row.payload if isinstance(row.payload, dict) else {}
    raw_artifact_type = payload.get("artifact_type")
    artifact_type = (
        raw_artifact_type
        if isinstance(raw_artifact_type, str) and raw_artifact_type.strip()
        else row.deliverable_type.value
    )
    return RunPartialDeliverable(
        id=row.id,
        artifact_type=artifact_type,
        summary=_opt_str(payload.get("summary")),
        channel=_opt_str(payload.get("channel")),
        external_ref=_opt_str(payload.get("external_ref")),
        created_at=row.created_at,
    )


def _build_timeline(
    activity_rows: list[ExecutionRunActivity],
    verification: VerificationResult | None,
    deliverable_id: uuid.UUID | None,
    deliverable_created_at: datetime | None,
    language: str = "en",
) -> tuple[list[RunActivity], str]:
    """Build the run's STORY timeline (oldest-first) + its source tag.

    Prefers REAL :class:`ExecutionRunActivity` rows (``timeline_source ==
    "activities"``). When none exist, DERIVES a calm timeline from the rows we
    already carry — the latest verification + the resulting deliverable (the
    DEFER fallback; ``timeline_source == "derived"``). Surfaces only what the
    schema actually stores — no fabricated per-step token traces.
    """
    if activity_rows:
        events: list[RunActivity] = []
        for row in activity_rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            label = _activity_label(row.activity_type, payload, language)
            if label is None:
                continue
            events.append(
                RunActivity(type=row.activity_type, label=label, created_at=row.created_at)
            )
        return events, "activities"

    # Derived fallback: synthesize from the verification + deliverable we have.
    derived: list[RunActivity] = []
    if verification is not None:
        label = _activity_label("verify", {"outcome": verification.outcome.value}, language)
        if label is not None:
            derived.append(
                RunActivity(type="verify", label=label, created_at=verification.created_at)
            )
    if deliverable_id is not None and deliverable_created_at is not None:
        derived.append(
            RunActivity(
                type="deliver",
                label=(
                    "산출물을 만들었어요" if _lang(language) == "ko" else "Produced a deliverable"
                ),
                created_at=deliverable_created_at,
            )
        )
    derived.sort(key=lambda e: e.created_at)
    return derived, "derived"


async def build_run_detail(
    *,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    session: AsyncSession,
    runs: RunRepository,
    decisions: DecisionRepository,
    deliverables: DeliverableRepository,
) -> RunDetailResponse | None:
    """Compose the inspectable run-detail view, or ``None`` if the run is not
    in this workspace.

    Bundles the run's trigger context (defensively read out of the free-form
    ``payload``), its paused-run Decisions (the blocking questions the founder
    resolves via /api/v1/checkpoints), the latest VerificationResult outcome,
    and the resulting Deliverable id (so a caller can link to its Delivery
    Report). A run with a sparse payload degrades to a calm minimal detail
    rather than erroring.
    """
    run = await runs.get(run_id)
    if run is None or run.workspace_id != workspace_id:
        # ``None`` rather than an HTTP error: the caller decides how "unknown
        # here" is spelled (REST 404, MCP ToolError). A cross-workspace id is
        # indistinguishable from an unknown one, so neither surface leaks.
        return None

    decision_rows = await decisions.list_by_run(run_id, workspace_id)

    latest_verification_stmt = (
        select(VerificationResult)
        .where(
            VerificationResult.run_id == run_id,
            VerificationResult.workspace_id == workspace_id,
        )
        .order_by(VerificationResult.created_at.desc())
        .limit(1)
    )
    verification_row = (await session.execute(latest_verification_stmt)).scalars().first()

    # D6 — Deliverables for this run: the verified-final + any mid-loop partials.
    # We load all rows once and split by ``payload["kind"] == PARTIAL_DELIVERABLE_KIND``
    # so ``deliverable_id`` returns the verified-final regardless of timing (a
    # late-arriving partial must NOT shadow the terminal on the Run-view), and
    # ``partial_deliverables`` returns the streaming list (oldest-first, the
    # order they were emitted by the loop).
    deliverable_rows = await deliverables.list_by_run(run_id, workspace_id)
    partial_rows: list[Deliverable] = []
    final_rows: list[Deliverable] = []
    for row in deliverable_rows:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if payload.get("kind") == PARTIAL_DELIVERABLE_KIND:
            partial_rows.append(row)
        else:
            final_rows.append(row)
    # When multiple non-partial Deliverables exist (legacy / future), the
    # most-recent one wins — matches the prior "latest" semantics for non-partial
    # rows and keeps the verified terminal nondegenerate.
    final_row = final_rows[-1] if final_rows else None
    deliverable_id = final_row.id if final_row is not None else None
    deliverable_created_at = final_row.created_at if final_row is not None else None

    partial_deliverables = [_partial_deliverable(row) for row in partial_rows]

    # The run's STORY: meaningful activity rows, oldest-first.
    activities_stmt = (
        select(ExecutionRunActivity)
        .where(
            ExecutionRunActivity.run_id == run_id,
            ExecutionRunActivity.workspace_id == workspace_id,
        )
        .order_by(ExecutionRunActivity.created_at.asc())
    )
    activity_rows = list((await session.execute(activities_stmt)).scalars().all())
    # The run's STORY speaks the workspace's language — localized at the
    # producer, like every notification sentence. Absent row → "en".
    language = await load_workspace_language(session, workspace_id)
    activities, timeline_source = _build_timeline(
        activity_rows, verification_row, deliverable_id, deliverable_created_at, language
    )

    # L2 (#9): for a terminal-failed run, surface WHY — the latest history
    # ``reason`` for the FAILED / CANCELLED transition (the loop's failure
    # summary / the discard context). The founder sees the cause + a Retry
    # affordance instead of a blank "nothing to do". None for non-failed runs.
    failure_reason: str | None = None
    if run.status in (RunStatus.FAILED, RunStatus.CANCELLED):
        history_stmt = (
            select(ExecutionRunHistory)
            .where(
                ExecutionRunHistory.run_id == run_id,
                ExecutionRunHistory.workspace_id == workspace_id,
                ExecutionRunHistory.to_status.in_((RunStatus.FAILED, RunStatus.CANCELLED)),
            )
            .order_by(ExecutionRunHistory.created_at.desc())
            .limit(1)
        )
        history_row = (await session.execute(history_stmt)).scalars().first()
        failure_reason = history_row.reason if history_row is not None else None

    return RunDetailResponse(
        id=run.id,
        workspace_id=run.workspace_id,
        product_id=run.product_id,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        trigger=_trigger_context(run.payload),
        decisions=[
            RunDecision(
                id=d.id,
                decision=d.decision,
                question=_question_text(d),
                rationale=d.rationale,
                status=d.status,
                resolution=d.resolution,
                created_at=d.created_at,
            )
            for d in decision_rows
        ],
        verification=(
            RunVerification(
                id=verification_row.id,
                outcome=verification_row.outcome,
                created_at=verification_row.created_at,
            )
            if verification_row is not None
            else None
        ),
        deliverable_id=deliverable_id,
        partial_deliverables=partial_deliverables,
        activities=activities,
        timeline_source=timeline_source,
        failure_reason=failure_reason,
    )


__all__ = [
    "_activity_label",
    "build_run_detail",
    "_build_timeline",
    "_intent_of",
    "_partial_deliverable",
    "_question_text",
    "_trigger_context",
]
