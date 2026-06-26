"""Report enrichment helpers for the deliverable report (R1 + R8 + R10).

Kept in its own thin sub-file so :mod:`.proof` stays under the D35 250-LOC
adapter ceiling. ``report_narrative_for`` lazily generates the plain-language
"what this did" (R1); ``held_delivery_item_for`` finds a pending Safe-Mode item
for the footer (R8); ``split_knowledge`` separates the knowledge the run
CONSULTED (referenced) from the notes it WROTE (added), keeping the report's
"참고한 지식" and "추가한 지식" groups distinct (R10).
"""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workers.db import SettleDrainRow
from backend.workflow.infrastructure.db import Deliverable, ExecutionRun
from backend.workflow.infrastructure.delivery.db import (
    SafeModeQueueItemRow,
    SafeModeStatus,
)

from ._schemas import WrittenNote

logger = structlog.get_logger(__name__)


async def held_delivery_item_for(
    session: AsyncSession, deliverable_id: uuid.UUID, workspace_id: uuid.UUID
) -> uuid.UUID | None:
    """The id of the PENDING Safe-Mode held delivery for this deliverable, if any
    (R8). When set, the report footer offers Approve & ship / Decline on it —
    mirroring the Brief's "Needs you" card — instead of Rollback. ``None`` when
    nothing is held (a shipped run, or one already denied/expired)."""
    stmt = (
        select(SafeModeQueueItemRow.id)
        .where(
            SafeModeQueueItemRow.deliverable_id == deliverable_id,
            SafeModeQueueItemRow.workspace_id == workspace_id,
            SafeModeQueueItemRow.status == SafeModeStatus.PENDING,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


_WRITTEN_MAX = 12
# A retrieved-knowledge statement that points at a raw vault SEEDLING note —
# "Related note — garden/seedling/settle-<slug>.md". These come from the
# SemanticNoteRetriever's search over garden embeddings (seedlings only). They
# are the EPISODIC layer (per-run observations), not the canonical knowledge the
# concept graph shows — so the report DROPS them to stay concept-centric (R16).
_RELATED_NOTE_RE = re.compile(r"^related note\s*[—–-]\s*(.+\.md)$", re.IGNORECASE)


def _is_seedling_note_ref(reference: str) -> bool:
    """True for a "Related note — <path>.md" statement (a raw seedling hit from
    the semantic note search). Concept / decision / rejection statements are not
    note refs → False, and stay in the report's referenced knowledge."""
    return _RELATED_NOTE_RE.match(reference.strip()) is not None


def _note_title(node_ref: str) -> str:
    """A readable title for a written note's vault path — the last segment,
    de-slugged ("garden/seedling/settle-add-a-title-case-helper.md" → "Add a
    title case helper"). The settle- prefix the garden writer adds is stripped."""
    file = node_ref.rsplit("/", 1)[-1]
    file = re.sub(r"\.md$", "", file, flags=re.IGNORECASE)
    file = re.sub(r"^settle-", "", file)
    text = file.replace("-", " ").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else node_ref


def _ws_relative(node_ref: str, workspace_id: uuid.UUID) -> str:
    """The vault-relative path the note viewer expects ("garden/seedling/x.md")
    from a settle_drains ``node_ref`` (ABSOLUTE in prod:
    ``/app/var/vault/<region>/<ws>/garden/...``). Splits on the workspace-id
    segment; an already-relative ref passes through (just trims a leading /)."""
    marker = f"/{workspace_id}/"
    if marker in node_ref:
        return node_ref.split(marker, 1)[1]
    return node_ref.lstrip("/")


async def split_knowledge(
    session: AsyncSession,
    run_id: uuid.UUID,
    workspace_id: uuid.UUID,
    references: list[str],
) -> tuple[list[str], list[WrittenNote]]:
    """Split the report's knowledge into (referenced, written) — keeping "참고한
    지식" and "추가한 지식" distinct AND concept-centric (R16).

    REFERENCED = the PROMOTED/canonical knowledge the run drew on — the retrieved
    CONCEPTS (graph anchors) + prior decisions/rejections. The raw seedling
    "Related note —" hits (the SemanticNoteRetriever's search over garden
    seedlings) are DROPPED: they're the episodic layer, NOT what the concept
    graph shows, so surfacing them made the report inconsistent with the graph
    (founder: the graph's mature notes are the main axis). The seedling search
    still feeds the verify contract — this only trims the founder-facing report.

    WRITTEN = the notes THIS run itself added, from ``settle_drains`` (run_id →
    node_ref): a de-slugged ``title`` + the vault-relative ``path`` so the chip
    deep-links to the note viewer. This is the run's own contribution (a fresh
    seedling), distinct from referenced knowledge; empty until the drain runs.
    """
    stmt = select(SettleDrainRow.node_ref).where(
        SettleDrainRow.run_id == run_id,
        SettleDrainRow.workspace_id == workspace_id,
        SettleDrainRow.node_ref.is_not(None),
    )
    written_paths = [p for p in (await session.execute(stmt)).scalars().all() if p]

    # Concept-centric: keep concepts + decisions/rejections; drop the raw
    # seedling note hits (they're the episodic layer, not the graph's canon).
    referenced = [r for r in references if not _is_seedling_note_ref(r)]

    written: list[WrittenNote] = []
    seen: set[str] = set()
    for node_ref in written_paths:
        title = _note_title(node_ref)
        if title and title not in seen:
            seen.add(title)
            written.append(WrittenNote(title=title, path=_ws_relative(node_ref, workspace_id)))
    return referenced, written[:_WRITTEN_MAX]


async def report_narrative_for(
    session: AsyncSession,
    row: Deliverable,
    run: ExecutionRun | None,
    request: str | None,
    verified: bool,
    workspace_id: uuid.UUID,
) -> str | None:
    """The cached / lazily-generated "what this did" narrative for the report."""
    payload = row.payload if isinstance(row.payload, dict) else {}
    cached = payload.get("narrative")
    if isinstance(cached, str) and cached.strip():
        return cached.strip()
    # Only spend a generation on a verified deliverable with something to describe.
    if not verified:
        return None
    summary = payload.get("summary") if isinstance(payload.get("summary"), str) else None
    diff = payload.get("diff") if isinstance(payload.get("diff"), str) else None
    intent: str | None = None
    if run is not None and isinstance(run.payload, dict):
        frame = run.payload.get("frame")
        if isinstance(frame, dict):
            intent = frame.get("framed_intent") or frame.get("summary_title")
    intent = intent or request
    if not (summary or diff or intent):
        return None
    from backend.workflow.application.report_narrative import (  # noqa: PLC0415 — lazy
        ReportNarrativeService,
    )

    service = ReportNarrativeService(session, settings=get_settings())
    narrative = await service.narrate(
        workspace_id=workspace_id, intent=intent, summary=summary, diff=diff
    )
    if not narrative:
        return None
    # Cache on the deliverable payload (re-assign so SQLAlchemy detects the JSON
    # change). Soft: a commit hiccup just means we regenerate next view.
    new_payload = dict(payload)
    new_payload["narrative"] = narrative
    row.payload = new_payload
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 — caching is best-effort, never breaks the read
        logger.warning("report_narrative_cache_failed", deliverable_id=str(row.id), exc_info=True)
    return narrative


__all__ = ["held_delivery_item_for", "report_narrative_for", "split_knowledge"]
