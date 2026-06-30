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
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import structlog

from backend.knowledge.graph.writer import GardenNote

from ._llm_compile import clean_entities, clean_tags

if TYPE_CHECKING:
    from backend.knowledge.canonicalization.service import CanonicalizationService
    from backend.knowledge.graph.writer import GardenWriter

logger = structlog.get_logger(__name__)


_REQUIRED_ACTION_FIELDS = {"action", "title", "content", "reason"}


# Lift E20 — the new prompt classifies every emitted note into one of
# four reusable-knowledge kinds. Notes that don't fit (codebase
# descriptions, file catalogs, boilerplate) are returned as an empty
# array. The constants are exposed so the prompt body + validator
# share one source of truth.
NOTE_KIND_PATTERN: str = "Pattern"
NOTE_KIND_PRINCIPLE: str = "Principle"
NOTE_KIND_TECH_INSIGHT: str = "TechInsight"
NOTE_KIND_DOMAIN_MODEL: str = "DomainModel"

VALID_NOTE_KINDS: frozenset[str] = frozenset(
    {
        NOTE_KIND_PATTERN,
        NOTE_KIND_PRINCIPLE,
        NOTE_KIND_TECH_INSIGHT,
        NOTE_KIND_DOMAIN_MODEL,
    }
)


@dataclass
class UpdateAction:
    """A single update/create/append action planned by the LLM.

    Lift E20 added the optional ``note_kind`` field — when the new
    Graphify-inspired prompt set the action's ``type`` to one of the
    four reusable-knowledge kinds (Pattern / Principle / TechInsight /
    DomainModel), the validator landed it here and the executor passes
    it through to ``GardenNote.note_type`` so the kind shows up in the
    note's YAML frontmatter as ``type: Pattern``.

    ``None`` means "the LLM didn't classify" — the legacy settle prompt
    still works without the ``type`` field.
    """

    action: Literal["update", "append", "create"]
    target_path: str | None
    title: str
    content: str
    reason: str
    tags: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    note_kind: str | None = None


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
    # Lift E8 Bug 2 — count of chunks that raised inside ``compile_batch``'s
    # per-chunk try/except. Surfaces the silent-fail signal callers (today the
    # product-bootstrap runtime) need to decide ``failed`` vs ``complete``
    # when ``notes_created + notes_updated == 0``: a real no-op repo has
    # ``chunk_failures == 0`` while an executor-without-redis bootstrap shows
    # ``chunk_failures == chunk_count``.
    chunk_failures: int = 0


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
    """Check that raw action dict has all required fields.

    Lift E20 added an OPTIONAL ``type`` field. When present, it MUST be
    one of :data:`VALID_NOTE_KINDS`; an invalid value drops the action
    (the LLM invented a kind the schema doesn't recognize, and we won't
    let it poison the vault). When absent, the action is accepted as
    legacy-schema — the settle pipeline still uses that.
    """
    if not isinstance(raw, dict):
        return False
    missing = _REQUIRED_ACTION_FIELDS - raw.keys()
    if missing:
        logger.debug("ingest_compile_action_missing_fields", missing=list(missing))
        return False
    if raw["action"] not in ("create", "update", "append"):
        return False
    if raw["action"] in ("update", "append") and not raw.get("target_path"):
        return False
    # Lift E20 — ``type`` is optional. When present, must be one of the
    # four reusable-knowledge kinds.
    if "type" in raw and raw["type"] is not None:
        type_value = raw["type"]
        if not isinstance(type_value, str) or type_value not in VALID_NOTE_KINDS:
            logger.debug("ingest_compile_action_bad_type", type=type_value)
            return False
    return True


#: Cap on the founding-note body distilled onto an ingest-auto-created concept,
#: so a long note can't blow out the concept hub. Bounded substance, not a dump.
_MAX_CONCEPT_SEED_BODY = 600


def _concept_seed_body(content: str) -> str | None:
    """A bounded excerpt of the founding note to seed an auto-created concept's
    body (import-pipeline K1 fix). ``None`` when the note carries no usable prose
    — better a title-only concept than a body of whitespace."""
    text = " ".join((content or "").split()).strip()
    if not text:
        return None
    return text[:_MAX_CONCEPT_SEED_BODY].rstrip()


async def canonicalize_tags(
    canon_service: CanonicalizationService | None,
    tags: list[str],
    *,
    raw_source: str,
    note_type: str | None = None,
    initial_body: str | None = None,
) -> list[str]:
    """Resolve cleaned tags to canonical concept ids (Handoff §11).

    Tags that resolve to existing concepts (or auto-create a new one)
    land in the garden note. Tags returning None
    (ambiguous/blocked/pending_candidate/auto-apply-failed) are dropped
    per spec — their evidence lives on the action/proposal record.

    Lift E27 — when the caller knows the note's E20 ``type`` field (the
    ingest plan has it in ``raw_action['type']``), thread it onto the
    auto-create-concept path so the concept inherits the kind in its
    frontmatter (E26 wire). Pre-E27 this path always omitted the type
    so every ingest-auto-created concept was untyped even when the
    seedling note that triggered it had one.

    Import-pipeline K1 fix — ``initial_body`` (the founding note's distilled
    excerpt) is threaded onto the auto-create-concept path so the new concept is
    born *substantive*, not an empty ``# Title`` shell. Ignored when the tag
    resolves to an existing concept (its body is owned by its own history).
    """
    if canon_service is None or not tags:
        return tags
    canonical: list[str] = []
    seen: set[str] = set()
    for raw_tag in tags:
        try:
            resolved = await canon_service.resolve_and_canonicalize(
                raw_tag,
                raw_source=raw_source,
                note_type=note_type,
                initial_body=initial_body,
            )
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
        # Lift E20 — accept the optional ``type`` field; validate_action
        # has already filtered out any invalid value, so just pass it
        # through. ``None`` keeps legacy behavior (no kind on the note).
        raw_type = raw_action.get("type")
        note_kind = raw_type if isinstance(raw_type, str) and raw_type in VALID_NOTE_KINDS else None
        # Lift E27 — thread the note's type into canonicalize_tags so the
        # auto-created concept inherits the same kind (E26 wire).
        tags = await canonicalize_tags(
            canon_service,
            tags,
            raw_source="ingest-compiler",
            note_type=note_kind,
            # Import-pipeline K1 fix — seed an auto-created concept's body with
            # the founding note's substance so it is not an empty title shell.
            initial_body=_concept_seed_body(raw_action["content"]),
        )
        # Lift E20 — the new prompt names the field ``wikilinks`` (strict
        # subset of content); the legacy prompt called it ``entities``.
        # Accept either; clean_entities enforces the in-content invariant.
        raw_links = raw_action.get("wikilinks") or raw_action.get("entities") or []
        entities = clean_entities(raw_links, raw_action["content"])

        action = UpdateAction(
            action=raw_action["action"],
            target_path=raw_action.get("target_path"),
            title=raw_action["title"],
            content=raw_action["content"],
            reason=raw_action["reason"],
            tags=tags,
            entities=entities,
            related=raw_action.get("related", []),
            note_kind=note_kind,
        )

        try:
            if action.action == "create":
                await writer.write_garden(
                    GardenNote(
                        title=action.title,
                        content=action.content,
                        source="ingest-compiler",
                        tags=action.tags,
                        entities=action.entities,
                        related=action.related,
                        # Lift E20 — when the new prompt classified, the
                        # kind lands in frontmatter as ``type: <Kind>``.
                        # ``None`` keeps the legacy "no type" behavior.
                        note_type=action.note_kind,
                    )
                )
                notes_created += 1
            elif action.action == "update" and action.target_path:
                await writer.update_note(action.target_path, action.content)
                notes_updated += 1
            elif action.action == "append" and action.target_path:
                await writer.append_to_note(action.target_path, action.content)
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
        # Import-pipeline noise fix — we NO LONGER generate an empty stub node
        # for every ``[[Name]]`` mention (the E20 auto-stub explosion). A node
        # exists only when it has substance: a concept the ingest canonicalizes
        # (now with a body, above) or one a recurring pattern promotes. A
        # wikilink whose target has no node yet simply dangles until the entity
        # earns a node — the digital-garden "no empty stubs" model.

    return CompileResult(
        actions_taken=actions_taken,
        notes_updated=notes_updated,
        notes_created=notes_created,
    )
