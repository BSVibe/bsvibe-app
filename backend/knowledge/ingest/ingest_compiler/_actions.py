"""Action execution + supporting data classes for :mod:`ingest_compiler`.

Lift L3 (v8 §17.6) split this off the orchestration class to keep
``_compiler.py`` under 400 LOC. The module owns:

- ``UpdateAction`` / ``CompileResult`` / ``IngestBatchRecord`` /
  ``IngestBatchRecorder`` — the data shapes exchanged across the compile
  surface.
- ``execute_plan`` — turn a validated LLM plan into garden writes.
- Stub creation, tag canonicalization, action validation.

The chunk-loop orchestrator (in :mod:`._compiler`) calls ``execute_plan``
once per chunk with that chunk's parsed plan. Nothing in here knows about
chunks or retrieval — those concerns live one level up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import structlog

from backend.knowledge.graph.writer import GardenNote

from ._llm_compile import _WIKILINK_PATTERN, clean_entities, clean_tags

if TYPE_CHECKING:
    from backend.knowledge.canonicalization.service import CanonicalizationService
    from backend.knowledge.graph.writer import GardenWriter

logger = structlog.get_logger(__name__)


_REQUIRED_ACTION_FIELDS = {"action", "title", "content", "reason"}


@dataclass
class UpdateAction:
    """A single update/create/append action planned by the LLM."""

    action: Literal["update", "append", "create"]
    target_path: str | None
    title: str
    content: str
    reason: str
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)


@dataclass
class CompileResult:
    """Result of an ingest compilation."""

    actions_taken: list[UpdateAction]
    notes_updated: int
    notes_created: int
    seed_path: str = ""
    llm_calls: int = 1
    # Telemetry for the ``ingest_batches`` analytics row (see
    # :class:`backend.knowledge.ingest.db.IngestBatch`). ``seed_count`` is
    # the number of input :class:`BatchItem`s; ``elapsed_ms`` the wall-clock
    # cost of the whole batch compile. Populated by ``compile_batch``.
    seed_count: int = 0
    elapsed_ms: int = 0


@dataclass(frozen=True)
class IngestBatchRecord:
    """The data needed to persist one ``ingest_batches`` analytics row.

    Decouples :class:`IngestCompiler` from the DB: the compiler hands this
    plain record to an optional :class:`IngestBatchRecorder` seam, and the
    request-handler glue (a later chunk) backs that seam with a SQLAlchemy
    writer. Keeping the row write behind a Protocol means the compiler core
    imports no session machinery and stays unit-testable with a fake.
    """

    seed_source: str
    seed_count: int
    notes_created: int
    notes_updated: int
    llm_calls: int
    chunk_count: int
    chunk_failures: int
    elapsed_ms: int


@runtime_checkable
class IngestBatchRecorder(Protocol):
    """Persists an :class:`IngestBatchRecord` (the per-batch analytics row).

    Production binds this to a writer over
    :class:`backend.knowledge.ingest.db.IngestBatch` (workspace_id + region
    come from the same :class:`~backend.knowledge.factory.KnowledgeFactory`
    boundary that scoped the vault). ``None`` keeps the row write optional —
    a missing recorder must never break ingest.
    """

    async def record(self, record: IngestBatchRecord) -> None: ...


def empty_compile_result() -> CompileResult:
    return CompileResult(actions_taken=[], notes_updated=0, notes_created=0)


def validate_action(raw: dict[str, Any]) -> bool:
    """Check that raw action dict has all required fields."""
    if not isinstance(raw, dict):
        return False
    missing = _REQUIRED_ACTION_FIELDS - raw.keys()
    if missing:
        logger.debug("ingest_compile_action_missing_fields", missing=list(missing))
        return False
    if raw["action"] not in ("create", "update", "append"):
        return False
    return not (raw["action"] in ("update", "append") and not raw.get("target_path"))


async def canonicalize_tags(
    canon_service: CanonicalizationService | None,
    tags: list[str],
    *,
    raw_source: str,
) -> list[str]:
    """Resolve cleaned tags to canonical concept ids (Handoff §11).

    Tags that resolve to existing concepts (or auto-create a new one)
    land in the garden note. Tags returning None
    (ambiguous/blocked/pending_candidate/auto-apply-failed) are dropped
    per spec — their evidence lives on the action/proposal record.
    """
    if canon_service is None or not tags:
        return tags
    canonical: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        try:
            resolved = await canon_service.resolve_and_canonicalize(raw_tag, raw_source=raw_source)
        except Exception as exc:  # noqa: BLE001 — never abort ingest on resolve error
            logger.warning(
                "ingest_compile_canonicalize_failed",
                raw_tag=raw_tag,
                error=str(exc),
            )
            continue
        if resolved is None or resolved in seen:
            continue
        canonical.append(resolved)
        seen.add(resolved)
    return canonical


async def ensure_entity_stubs(
    writer: GardenWriter,
    entities: list[str],
    mentioned_in: Path | None,
) -> None:
    """Best-effort: create / refresh a stub for every ``[[Name]]`` mentioned.

    Failures are logged but never propagated — a single bad entity (e.g.
    slug that escapes vault boundary) must not abort the whole compile.
    """
    if not mentioned_in:
        return
    for wikilink in entities:
        match = _WIKILINK_PATTERN.match(wikilink.strip())
        if not match:
            continue
        name = match.group(1).strip()
        try:
            await writer.ensure_entity_stub(name, mentioned_in)
        except (OSError, ValueError) as exc:
            logger.warning(
                "ingest_compile_entity_stub_failed",
                name=name,
                error=str(exc),
            )


async def execute_plan(
    writer: GardenWriter,
    canon_service: CanonicalizationService | None,
    plan: list[dict[str, Any]],
    max_updates: int,
) -> CompileResult:
    """Execute the planned actions, capped by ``max_updates``."""
    actions_taken: list[UpdateAction] = []
    notes_created = 0
    notes_updated = 0

    for raw_action in plan[:max_updates]:
        if not validate_action(raw_action):
            continue

        tags = clean_tags(raw_action.get("tags") or [])
        tags = await canonicalize_tags(canon_service, tags, raw_source="ingest-compiler")
        entities = clean_entities(raw_action.get("entities") or [], raw_action["content"])

        action = UpdateAction(
            action=raw_action["action"],
            target_path=raw_action.get("target_path"),
            title=raw_action["title"],
            content=raw_action["content"],
            reason=raw_action["reason"],
            tags=tags,
            entities=entities,
            related=raw_action.get("related", []),
        )

        try:
            if action.action == "create":
                written_path = await writer.write_garden(
                    GardenNote(
                        title=action.title,
                        content=action.content,
                        source="ingest-compiler",
                        tags=action.tags,
                        entities=action.entities,
                        related=action.related,
                    )
                )
                notes_created += 1
            elif action.action == "update" and action.target_path:
                written_path = await writer.update_note(action.target_path, action.content)
                notes_updated += 1
            elif action.action == "append" and action.target_path:
                written_path = await writer.append_to_note(action.target_path, action.content)
                notes_updated += 1
            else:
                logger.warning("ingest_compile_invalid_action", action=action.action)
                continue
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.warning(
                "ingest_compile_action_failed",
                action=action.action,
                title=action.title,
                error=str(exc),
            )
            continue

        actions_taken.append(action)
        # Ensure every wikilink target has a real vault file so the
        # graph extractor's ``WIKILINK_RE`` sweep finds nodes on both
        # ends. Cleaned by ``clean_entities`` already, so each item
        # is a valid ``[[Name]]`` actually present in the body.
        await ensure_entity_stubs(writer, action.entities, written_path)

    return CompileResult(
        actions_taken=actions_taken,
        notes_updated=notes_updated,
        notes_created=notes_created,
    )
