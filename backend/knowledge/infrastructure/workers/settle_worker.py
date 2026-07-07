"""SettleWorker — drain ``settle`` activities into the BSage graph.

Workflow §4: ``worker-settle`` is the **BSage write subscriber**. The agent
loop (:class:`backend.workflow.application.agent_loop.RunOrchestrator`) records a
``settle``-class :class:`~backend.workflow.infrastructure.db.ExecutionRunActivity` for every
verified work step — the continuous "writes knowledge" side channel. This
worker drains those rows into each workspace's BSage vault, completing the
*learning half* of the §5 trust ratchet (BSage knowledge is one-way and
monotonically accumulates).

Design notes
------------
* **DB-polling, not Redis Streams** — same Phase 1 justification as
  :class:`~backend.workflow.infrastructure.workers.delivery_worker.DeliveryWorker`. The Redis
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
* **Multi-server safety (Lift J / v8 §11).** Two layers:

  1. **Row-claim** — :func:`build_settle_claim_stmt` selects with
     ``FOR UPDATE SKIP LOCKED`` so two instances drain disjoint subsets
     of the un-drained activity rows (SQLite ignores the hint at the
     dialect level; PG honours it).
  2. **Per-workspace promote-lease** —
     :meth:`SettleWorker._promote_affected` wraps each workspace's
     promote call in
     :func:`~backend.workflow.infrastructure.lease.try_workspace_promote_lock`
     so two servers don't run the same workspace's promoter concurrently.
     The skip is a no-op (the OTHER server is promoting; the promoter is
     idempotent so the missed call is recovered on the next drain).

* **Promotion closes the §5 ratchet loop.** Depositing the raw observation is
  only the *learning* half. After a drain batch writes garden notes, the
  worker runs a :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
  per *affected* workspace so recurring patterns get promoted into canonical
  anchors (the "wall"). The promoter is idempotent and honours the
  workspace's canonicalization policy (Safe-Mode default → queued proposals;
  permissive → applied anchors), so a per-batch run is safe. Promotion is the
  *derived* step: a promotion failure is soft (logged + skipped) and never
  reverts a settle write or breaks the drain — the settlement notes remain the
  source of truth. The promoter is constructed against the SAME per-workspace
  vault boundary the sink uses (``<vault_root>/<region>/<workspace_id>/`` via
  the :class:`~backend.knowledge.factory.KnowledgeFactory` convention) behind a
  :class:`PromoterFactory` seam so the binding stays testable.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from backend.knowledge.canonicalization.promotion import ConceptFramer
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.identity.workspaces_db import WorkspaceRow
from backend.knowledge.extraction.worth_remembering import (
    RememberableKnowledge,
    is_inherently_notable,
)
from backend.workers.base import BaseWorker
from backend.workers.db import SettleDrainRow
from backend.workflow.infrastructure.db import ExecutionRunActivity
from backend.workflow.infrastructure.lease import (
    release_workspace_promote_lock,
    try_workspace_promote_lock,
)

logger = structlog.get_logger(__name__)

SETTLE_ACTIVITY_TYPE = "settle"

# Structural tags describe the *kind* of note, not what it is *about*; they are
# kept on every garden observation but the promoter intentionally drops them, so
# they must never appear as derived content tags either (Handoff §0.2).
_STRUCTURAL_TAGS: frozenset[str] = frozenset({"settle", "verified-run"})

# Cap on derived content tags per observation — conservative on purpose: a
# garden note should carry a handful of high-signal patterns, not a token dump.
_MAX_CONTENT_TAGS = 8

# Same normalization rule the canonicalization TagResolver uses (lowercase,
# collapse any non-[a-z0-9] run to a single hyphen, strip edge hyphens) so a
# derived tag is a candidate the promoter will actually pick up. Duplicated here
# (not imported) to keep the worker module's import surface cheap.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
# A normalized tag is only a valid concept-id candidate if it starts with a
# letter (Handoff §2: ``^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$``); a leading digit
# (e.g. from "v2") would be silently dropped by the promoter, so we drop it here.
_CONCEPT_ID_LEADING_RE = re.compile(r"^[a-z]")

# Salient-term extraction from the free-text summary. Tokens shorter than this
# (after normalization) are too generic to be useful patterns.
_MIN_SUMMARY_TOKEN_LEN = 3

# Worker-local stopword supplement — short, high-frequency function words and
# tense variants of common action verbs that the summary tokenizer would
# otherwise keep. This is the FALLBACK path's only lexical guard: the PRIMARY
# derivation now extracts LLM-committed entities (see :class:`EntityExtractor`),
# which excludes generic nouns structurally, so the open-ended filler deny-list
# is retired. This deliberately small set only trims the most obvious function
# words from the deterministic fallback used when no LLM is available — better
# to keep a borderline term than to over-prune signal.
_SUMMARY_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "onto",
        "this",
        "that",
        "these",
        "those",
        "have",
        "has",
        "had",
        "was",
        "were",
        "are",
        "but",
        "not",
        "via",
        "per",
        "out",
        "off",
        "its",
        "our",
        "your",
        "their",
        "added",
        "add",
        "fixed",
        "fix",
        "wired",
        "wire",
        "made",
        "make",
        "set",
        "got",
        "get",
        "ran",
        "run",
        "use",
        "used",
        "new",
        "now",
        "all",
        "any",
        "can",
        "did",
        "done",
        "then",
        "than",
        "step",
        "work",
    }
)


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
    # Stable run context threaded from the orchestrator's settle emission. The
    # product binding (slug/name) is the strongest cluster key — runs for the
    # SAME product should canonicalize together regardless of which files
    # changed — and the founder's ``intent_text`` (their own words) names what
    # the work was ABOUT. Both are deterministic stable inputs (NOT LLM output),
    # so using them as clustering signal honours the no-LLM-output-for-system-
    # fields rule. Absent for connector-inbound runs → graceful degradation to
    # summary + artifact_refs derivation only.
    product_slug: str | None = None
    product_name: str | None = None
    intent_text: str | None = None
    occurred_at: datetime | None = None
    # B11b: the settle payload's ``kind`` (``"decision_resolution"`` for an
    # answered checkpoint, absent for verified-work observations). Threaded
    # onto the absorbed garden note so downstream consumers — specifically the
    # :class:`~backend.knowledge.retrieval.resolved_decisions_retriever.ResolvedDecisionsRetriever`
    # — can recognise decision-resolution notes structurally instead of
    # parsing the free-text summary.
    kind: str | None = None
    # B11b: structured Q/A for a ``decision_resolution`` settlement, so the
    # absorbed note carries the founder's question + answer text as durable
    # extra-fields the retriever can read directly (instead of regex-parsing
    # the human-legible summary). ``None`` for verified-work observations.
    question: str | None = None
    answer: str | None = None
    # G1: the founder's free-text rejection reason for a ``negative_pattern``
    # settlement (a discard-with-reason). Threaded onto the absorbed note's
    # extra-fields so the
    # :class:`~backend.knowledge.retrieval.negative_pattern_retriever.NegativePatternRetriever`
    # reads the guidance directly. ``None`` for every other settlement kind.
    reason: str | None = None


class SettleSink(Protocol):
    """Absorbs a :class:`Settlement` into knowledge storage.

    Returns a reference to the written node (a vault path) or ``None`` when
    nothing was written. The worker is decoupled from BSage behind this
    Protocol so the drain bookkeeping is testable without a real vault.
    """

    async def absorb(self, settlement: Settlement) -> str | None: ...


class EntityExtractor(Protocol):
    """Extracts named entities from a piece of text — the PRIMARY tag source.

    Mirrors the surface of
    :meth:`backend.knowledge.ingest.ingest_compiler.IngestCompiler.extract_entity_names`:
    given seed text it returns the entity NAMES the LLM committed as
    ``[[wikilinks]]`` (guarded by the ``_clean_entities`` anti-hallucination
    gate). This is how BSage derives concepts — generic nouns ("work",
    "returns", "string") are excluded *structurally* (an entity must appear as a
    literal wikilink in the model's own prose), not via an open-ended deny-list.
    """

    async def extract_entity_names(self, text: str, *, label: str = ...) -> list[str]: ...


class ExtractorFactory(Protocol):
    """Builds an :class:`EntityExtractor` for one workspace/region, or ``None``.

    The settle sink calls this per settlement to derive concepts from extracted
    entities. Construction is the per-workspace boundary (the
    :class:`~backend.knowledge.factory.KnowledgeFactory` convention roots the
    extractor's vault at ``<vault_root>/<region>/<workspace_id>/``); the
    production factory also resolves the workspace's LLM model account. Returning
    ``None`` (no LLM / no active account / not configured) is the documented
    soft-fallback signal — the sink degrades to the deterministic
    :func:`derive_content_tags` heuristic and the settlement write still
    succeeds. Errors raised here are caught by the sink for the same reason.
    """

    async def __call__(self, *, region: str, workspace_id: uuid.UUID) -> EntityExtractor | None: ...


class MemoryExtractor(Protocol):
    """Judges whether a verified-work settlement left anything WORTH REMEMBERING.

    Founder directive (2026-07): a run only leaves a note when it produced a
    retrospective insight / a non-obvious learning / a user decision — NOT for
    routine work (adding a utility, fixing a typo). Given a :class:`Settlement`
    (intent + summary), the extractor runs the shared worth-remembering prompt
    (:func:`backend.knowledge.extraction.worth_remembering.worth_remembering_messages`)
    and returns the parsed
    :class:`~backend.knowledge.extraction.worth_remembering.RememberableKnowledge`
    to write, or ``None`` when there is nothing worth keeping (routine → no note).
    """

    async def extract(self, settlement: Settlement) -> RememberableKnowledge | None: ...


class MemoryExtractorFactory(Protocol):
    """Builds a :class:`MemoryExtractor` for one workspace/region, or ``None``.

    The settle sink calls this per verified-work settlement to judge whether the
    work is worth remembering. Construction resolves the workspace's routed LLM
    account (the settle-extract caller); returning ``None`` (no LLM / no active
    account / not configured) is the documented soft-fallback — a verified-work
    settlement with no extractor writes NOTHING (routine is the default outcome).
    Errors raised here are caught by the sink and treated the same way.
    """

    async def __call__(self, *, region: str, workspace_id: uuid.UUID) -> MemoryExtractor | None: ...


class WorkspacePromoter(Protocol):
    """Promotes a workspace's accumulated garden observations into canon.

    Mirrors the surface of
    :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
    that the worker depends on (``promote``), so the post-drain promotion step
    is testable without standing up a real canonicalization engine.
    """

    async def promote(self) -> object: ...


class PromoterFactory(Protocol):
    """Builds a :class:`WorkspacePromoter` for one workspace/region.

    Construction is the per-workspace vault boundary: the promoter is rooted at
    ``<vault_root>/<region>/<workspace_id>/`` (the
    :class:`~backend.knowledge.factory.KnowledgeFactory` convention), reusing
    the exact boundary the sink wrote to. ``safe_mode`` carries the workspace's
    canonicalization policy through to the engine (default strict → queued;
    permissive → applied). Returning ``None`` disables promotion for that
    workspace (e.g. promotion not configured).
    """

    def __call__(
        self, *, region: str, workspace_id: uuid.UUID, safe_mode: bool
    ) -> WorkspacePromoter | None: ...


class ConceptFramerFactory(Protocol):
    """Builds a :class:`~backend.knowledge.canonicalization.promotion.ConceptFramer`
    for one workspace (Lift 1b), or ``None`` when the workspace routed no model
    for ``knowledge.canonicalization``.

    Async because resolving the routed account is a DB read. ``None`` keeps the
    promoter deterministic (Lift 1 body) — the framing model is 100% user-routed,
    never hardcoded ([[bsvibe-no-implicit-routing]])."""

    async def __call__(self, *, region: str, workspace_id: uuid.UUID) -> ConceptFramer | None: ...


class NoteEmbedHook(Protocol):
    """Populate the pgvector note store from a freshly absorbed note (G5b).

    Called once per successfully absorbed + drained settlement with the
    settlement and the written note's ``node_ref`` (its vault path). The
    implementation resolves the workspace's embedder, computes + stores the
    embedding, and owns its OWN DB session + commit — so it is fully decoupled
    from the settle transaction. The worker invokes it soft-fail: any error is
    logged and swallowed, never reverting the settle write."""

    async def __call__(self, settlement: Settlement, node_ref: str) -> None: ...


class ReconcileHook(Protocol):
    """Backfill a workspace's un-embedded knowledge notes after promotion (Lift 2).

    A freshly promoted *active* concept carries body (Lift 1) but fires no
    vault-write event in the settle runtime (the
    :class:`~backend.knowledge.canonicalization.service.CanonicalizationService`
    runs without an event bus), so it would never be embedded — un-retrievable
    until a manual reconcile. The worker invokes this hook right after a promote
    pass that created a concept, with the workspace's region + id; the
    implementation resolves the embedder, walks the vault's knowledge layers,
    and embeds only the gap (idempotent — see
    :func:`~backend.knowledge.retrieval.reconcile.reconcile_embeddings`). It owns
    its OWN DB session + commit and is invoked soft-fail: any error is logged and
    swallowed, never reverting the promotion or the settle write."""

    async def __call__(self, *, region: str, workspace_id: uuid.UUID) -> object: ...


@dataclass(slots=True)
class SettleWorkerConfig:
    batch_size: int = 50
    poll_interval_s: float = 5.0
    # Fallback region when a workspace row carries none (matches
    # ``Settings.knowledge_default_region``). The runtime entrypoint should
    # pass the configured value.
    default_region: str = "us-1"


class KnowledgeSettleSink:
    """Production sink — writes a BSage garden note ONLY when the work is worth
    remembering (founder directive, 2026-07).

    Knowledge is NOT a work-history log. The sink gates every settlement:

    * **Inherently notable** (``decision_resolution`` / ``negative_pattern`` — a
      user CHOICE or a discard-with-reason) is ALWAYS written, no LLM needed
      (:func:`~backend.knowledge.extraction.worth_remembering.is_inherently_notable`).
    * **Verified work** must EARN its note through the worth-remembering
      extractor (:class:`MemoryExtractor`): a retrospective insight / non-obvious
      learning yields a note titled by its ``topic``; routine work (or no wired
      extractor) yields ``None`` — nothing written.

    When a note IS written it remains the §5 ratchet deposit that
    canonicalization later promotes; this sink only deposits the raw observation.
    """

    __slots__ = ("_vault_root", "_extractor_factory", "_memory_extractor")

    def __init__(
        self,
        *,
        vault_root: Path,
        extractor_factory: ExtractorFactory | None = None,
        memory_extractor: MemoryExtractorFactory | None = None,
    ) -> None:
        self._vault_root = vault_root
        # PRIMARY content-tag source: when wired, concepts come from LLM-extracted
        # entities (BSage's mechanism). ``None`` (tests / no LLM) keeps the pure
        # deterministic fallback — the sink never *requires* an LLM.
        self._extractor_factory = extractor_factory
        # The worth-remembering GATE for verified work. ``None`` (tests / no LLM)
        # means routine verified work writes NOTHING — the founder-directed
        # default. Inherently-notable settlements bypass this gate entirely.
        self._memory_extractor = memory_extractor

    async def absorb(self, settlement: Settlement) -> str | None:
        from backend.knowledge.factory import KnowledgeFactory  # noqa: PLC0415 — lazy heavy import
        from backend.knowledge.graph.writer import GardenNote  # noqa: PLC0415

        summary = settlement.summary.strip()
        # GATE (founder directive): decide whether this settlement is worth a note
        # and, if so, its title + body. Inherently-notable kinds (user decision /
        # discard-with-reason) are always kept with the descriptive settle title;
        # verified work must earn its note through the worth-remembering extractor
        # (topic-titled), and routine work earns none → return None (no write).
        if is_inherently_notable(settlement.kind):
            headline = summary.splitlines()[0][:80] if summary else "verified work step"
            title = f"Settle: {headline}"
            body = _observation_body(settlement, summary)
        else:
            memory = await self._extract_memory(settlement)
            if memory is None:
                logger.info(
                    "settle_sink_skipped_not_worth_remembering",
                    workspace_id=str(settlement.workspace_id),
                    run_id=str(settlement.run_id),
                    activity_id=str(settlement.activity_id),
                )
                return None
            title = memory.topic
            body = _memory_body(settlement, memory)

        factory = KnowledgeFactory(
            region=settlement.region,
            workspace_id=str(settlement.workspace_id),
            vault_root=self._vault_root,
        )
        writer = factory.writer()

        # Structural tags first (other consumers rely on them), then the content
        # tags so the GardenObservationPromoter has real candidates to cluster
        # across runs — closing the §5 ratchet loop. Content tags are de-duped
        # against structural ones already. PRIMARY: LLM-extracted entities (when
        # an extractor is wired + succeeds); soft-fallback: the deterministic
        # heuristic. Settlement is the source of truth, so derivation never breaks
        # the write.
        content_tags = await self._derive_tags(settlement)
        tags = ["settle", "verified-run", *content_tags]
        # B11b: a decision-resolution settlement gets an extra structural tag
        # so consumers can filter for it without parsing the body.
        if settlement.kind == "decision_resolution":
            tags.append("decision-resolution")
        # G1: a negative-pattern settlement (discard-with-reason) gets its own
        # structural tag so the NegativePatternRetriever filters on it cheaply.
        if settlement.kind == "negative_pattern":
            tags.append("negative-pattern")
        extra_fields: dict[str, object | None] = {
            "run_id": str(settlement.run_id),
            "activity_id": str(settlement.activity_id),
            "verified": settlement.verified,
            "artifact_refs": list(settlement.artifact_refs),
            "product_slug": settlement.product_slug,
            "product_name": settlement.product_name,
            "intent_text": settlement.intent_text,
        }
        if settlement.kind is not None:
            extra_fields["kind"] = settlement.kind
        if settlement.question is not None:
            extra_fields["question"] = settlement.question
        if settlement.answer is not None:
            extra_fields["answer"] = settlement.answer
        if settlement.reason is not None:
            extra_fields["reason"] = settlement.reason
        note = GardenNote(
            title=title,
            content=body,
            source="settle_worker",
            knowledge_layer="episodic",
            tags=tags,
            extra_fields=extra_fields,
        )
        path = await writer.write_garden(note)
        return str(path)

    async def _extract_memory(self, settlement: Settlement) -> RememberableKnowledge | None:
        """Run the worth-remembering gate over a verified-work settlement.

        Returns the :class:`RememberableKnowledge` to write, or ``None`` when the
        work is routine (nothing worth keeping). ``None`` is the DEFAULT and
        biased-for outcome: no factory wired, factory returns ``None`` (no routed
        account), the extractor's verdict is "not worth remembering", or the
        extraction raised — all degrade to "write nothing". A verified-work run
        only leaves a note when the extractor affirmatively names an insight.
        """
        if self._memory_extractor is None:
            return None
        try:
            extractor = await self._memory_extractor(
                region=settlement.region, workspace_id=settlement.workspace_id
            )
            if extractor is None:
                return None
            return await extractor.extract(settlement)
        except Exception:  # noqa: BLE001 — the gate is soft; a failure writes nothing
            logger.warning(
                "settle_sink_memory_extraction_failed",
                workspace_id=str(settlement.workspace_id),
                run_id=str(settlement.run_id),
                exc_info=True,
            )
            return None

    async def _derive_tags(self, settlement: Settlement) -> list[str]:
        """Content tags for the observation: extracted entities, else fallback.

        PRIMARY: run the workspace's :class:`EntityExtractor` over the verified
        work's summary + intent and use the LLM-committed entity names
        (normalized) as candidate concepts — BSage's mechanism, where generic
        nouns are excluded structurally. When the extractor RUNS successfully its
        result is AUTHORITATIVE — including an empty list, which the compile
        prompt documents as the COMMON case ("no reusable knowledge worth a
        concept"). We must NOT degrade an empty-but-successful extraction to the
        deterministic tokenizer: that injects generic intent/summary words
        (small, utility, create, common, …) as content tags which the promoter
        then auto-promotes to noise concepts (the 'utility'/'small' garden
        clutter). SOFT-FALLBACK to the deterministic :func:`derive_content_tags`
        heuristic fires ONLY when there is no LLM signal at all — no factory, no
        extractor, or an extraction error. The settlement is the source of truth;
        a derivation failure is logged + degraded, never raised, so it can't
        break the settle write.
        """
        if self._extractor_factory is None:
            return derive_content_tags(settlement)
        try:
            extractor = await self._extractor_factory(
                region=settlement.region, workspace_id=settlement.workspace_id
            )
            if extractor is None:
                return derive_content_tags(settlement)
            names = await extractor.extract_entity_names(_extraction_seed_text(settlement))
        except Exception:  # noqa: BLE001 — extraction is derived; never break the write
            logger.warning(
                "settle_sink_entity_extraction_failed",
                workspace_id=str(settlement.workspace_id),
                run_id=str(settlement.run_id),
                exc_info=True,
            )
            return derive_content_tags(settlement)
        # The extractor ran — trust its verdict, empty included. No deterministic
        # fallback here (that's the noise-concept source); structural tags alone
        # are correct for a run with no reusable knowledge.
        return _entity_names_to_tags(names)


def _extraction_seed_text(settlement: Settlement) -> str:
    """Assemble the seed text the entity extractor sees.

    The founder's ``intent_text`` (their own words naming what the work is ABOUT)
    leads, then the verified work summary. Both are deterministic inputs; the LLM
    only *names entities* within them (it does not invent system fields), so this
    honours the no-LLM-output-for-system-fields rule the same way BSage trusts
    LLM-extracted entities at ``confidence=INFERRED``.
    """
    parts = [p for p in (settlement.intent_text, settlement.summary.strip()) if p]
    return "\n\n".join(parts)


def _entity_names_to_tags(names: Iterable[str]) -> list[str]:
    """Normalize extracted entity names into capped, de-duped concept candidates.

    Each name is run through :func:`_normalize_tag` (same grammar the promoter
    requires), structural markers + non-conforming names are dropped, the product
    slug still leads as the strongest stable cluster key, and the result is
    de-duplicated (first-wins) and capped at :data:`_MAX_CONTENT_TAGS`.
    """
    tags: list[str] = []
    for name in names:
        if not isinstance(name, str):
            continue
        tag = _normalize_tag(name)
        if tag:
            tags.append(tag)
    deduped = list(dict.fromkeys(tags))  # first-wins, preserves order
    return deduped[:_MAX_CONTENT_TAGS]


def _memory_body(settlement: Settlement, memory: RememberableKnowledge) -> str:
    """Body for a worth-remembering verified-work note.

    The ``insight`` (what to remember + why) leads — this is the knowledge, not a
    work log. Stable run context (product binding + founder intent, both
    deterministic inputs, never LLM output) trails as provenance so the note says
    what the work was ABOUT, and the run id anchors it back to the activity.
    """
    lines = [memory.insight, ""]
    if settlement.product_slug or settlement.product_name:
        product = settlement.product_slug or settlement.product_name
        lines.append(f"Product: {product}")
    if settlement.intent_text:
        lines.append(f"Intent: {settlement.intent_text}")
    lines.append(f"Run: {settlement.run_id}")
    return "\n".join(lines)


def _observation_body(settlement: Settlement, summary: str) -> str:
    lines = [summary or "(no summary recorded)", ""]
    # Stable run context (product binding + founder intent) — recorded in the
    # note body so the observation says what the work was ABOUT, not just which
    # files moved. Both are deterministic inputs, never LLM output.
    if settlement.product_slug or settlement.product_name:
        product = settlement.product_slug or settlement.product_name
        lines.append(f"Product: {product}")
    if settlement.intent_text:
        lines.append(f"Intent: {settlement.intent_text}")
    if settlement.artifact_refs:
        lines.append("## Artifacts")
        lines.extend(f"- `{ref}`" for ref in settlement.artifact_refs)
        lines.append("")
    lines.append(f"Verified: {'yes' if settlement.verified else 'no'}")
    lines.append(f"Run: {settlement.run_id}")
    return "\n".join(lines)


def _normalize_tag(raw: str) -> str:
    """Normalize a raw token into a valid concept-id candidate, or ``""``.

    Mirrors :meth:`backend.knowledge.canonicalization.resolver.TagResolver.normalize`
    (lowercase, collapse non-alnum runs to a single hyphen, strip edge hyphens)
    and additionally rejects anything not starting with a letter, so every
    returned tag passes the Handoff §2 concept-id grammar the promoter requires.
    Structural tags (``settle`` / ``verified-run``) normalize to themselves and
    are rejected so they never re-enter as content tags.
    """
    normalized = _NON_ALNUM_RE.sub("-", raw.casefold()).strip("-")
    if not normalized or normalized in _STRUCTURAL_TAGS:
        return ""
    if not _CONCEPT_ID_LEADING_RE.match(normalized):
        return ""
    return normalized


def _tags_from_artifact_refs(artifact_refs: Iterable[str]) -> list[str]:
    """Derive stable identifiers from artifact paths (stems + basenames).

    Each ref is treated as a POSIX-ish path: every meaningful path component
    contributes its stem (``backend/auth/client.py`` → ``auth`` + ``client``).
    Recurring files across runs are exactly the patterns worth promoting, so the
    derived tags are deterministic stems — not full paths. Generic container
    components (``backend``/``src``/...) are kept only if they normalize cleanly;
    de-duplication happens at the call site so order is preserved.
    """
    tags: list[str] = []
    for ref in artifact_refs:
        if not isinstance(ref, str):
            continue
        # Normalize Windows separators, then split into path components.
        pure = PurePosixPath(ref.replace("\\", "/"))
        for part in pure.parts:
            stem = PurePosixPath(part).stem  # drop the file extension if any
            tag = _normalize_tag(stem)
            if tag:
                tags.append(tag)
    return tags


def _tags_from_summary(summary: str) -> list[str]:
    """Derive a few salient lowercase terms from the free-text summary.

    Deterministic heuristic, no LLM: tokenize on non-alnum, drop stopwords and
    short tokens, normalize each survivor into a concept-id candidate. Order
    follows first appearance in the summary; the caller dedupes + caps.
    """
    tags: list[str] = []
    for token in _NON_ALNUM_RE.split(summary.casefold()):
        if len(token) < _MIN_SUMMARY_TOKEN_LEN or token in _SUMMARY_STOPWORDS:
            continue
        tag = _normalize_tag(token)
        if not tag or tag in _SUMMARY_STOPWORDS:
            continue
        tags.append(tag)
    return tags


def _tags_from_product(product_slug: str | None, product_name: str | None) -> list[str]:
    """Derive the product cluster key from the run's product binding.

    The slug is the canonical stable binding (``^[a-z][a-z0-9-]*$`` already, so
    it normalizes to itself); the name is a defensive fallback when no slug is
    carried. At most ONE product tag is emitted (slug preferred) so the strong
    cluster key is not diluted by a near-duplicate name token.
    """
    for raw in (product_slug, product_name):
        if not isinstance(raw, str) or not raw.strip():
            continue
        tag = _normalize_tag(raw)
        if tag:
            return [tag]
    return []


def derive_content_tags(settlement: Settlement) -> list[str]:
    """Deterministically derive content tags from a :class:`Settlement`.

    Pure + offline (no LLM, no network). Tags are ordered by clustering strength
    so the strongest stable keys lead (and survive the cap):

    1. **Product** (slug, else name) — the strongest stable cluster key: runs
       for the same product should canonicalize together regardless of which
       files changed. At most one product tag.
    2. **Intent terms** — salient terms from the founder's ``intent_text``
       (their own words naming what the work is ABOUT).
    3. **Artifact-ref stems** — the recurring-file signal (PR #27).
    4. **Summary terms** — salient terms from the work summary (PR #27).

    The result is normalized to valid concept-id candidates, de-duplicated
    (first-wins, order preserved), structural tags excluded, and capped at
    :data:`_MAX_CONTENT_TAGS`. A settlement with no product, no intent, and no
    contentful summary/refs yields ``[]`` so the sink falls back to
    structural-only tags — the exact PR #27 graceful-degradation behaviour for
    connector-inbound runs.
    """
    ordered = _tags_from_product(settlement.product_slug, settlement.product_name)
    if settlement.intent_text:
        ordered.extend(_tags_from_summary(settlement.intent_text))
    ordered.extend(_tags_from_artifact_refs(settlement.artifact_refs))
    ordered.extend(_tags_from_summary(settlement.summary))
    deduped = list(dict.fromkeys(ordered))  # first-wins, preserves order
    return deduped[:_MAX_CONTENT_TAGS]


@dataclass(frozen=True, slots=True)
class _WorkspacePolicy:
    """Per-workspace settle context resolved from the ``workspaces`` row.

    ``safe_mode`` is the workspace's canonicalization policy (Safe-Mode default
    True → promotion queues proposals; False → it auto-applies anchors).
    """

    region: str
    safe_mode: bool


def build_garden_promoter_factory(
    *, vault_root: Path, framer_factory: ConceptFramerFactory | None = None
) -> PromoterFactory:
    """Production :class:`PromoterFactory` rooted at the shared vault boundary.

    Builds, per call, a :class:`~backend.knowledge.canonicalization.promotion.GardenObservationPromoter`
    over a :class:`~backend.knowledge.graph.storage.FileSystemStorage` rooted at
    ``<vault_root>/<region>/<workspace_id>/`` — the exact directory
    :class:`KnowledgeSettleSink` (via :class:`~backend.knowledge.factory.KnowledgeFactory`)
    wrote the garden observations to. ``safe_mode`` is threaded into the
    :class:`~backend.knowledge.canonicalization.service.CanonicalizationService`
    so the workspace's policy decides queue-vs-apply; the worker never overrides
    it. The returned factory is sync but builds a coroutine-driven promoter, so
    promotion itself runs inside the worker's ``await``.

    Lift 1b: ``framer_factory`` (when wired) is resolved per promote pass to a
    routed :class:`ConceptFramer` that distils new concept bodies. ``None`` keeps
    promotion fully deterministic.
    """

    def _factory(
        *, region: str, workspace_id: uuid.UUID, safe_mode: bool
    ) -> WorkspacePromoter | None:
        return _LazyGardenPromoter(
            vault_root=vault_root,
            region=region,
            workspace_id=workspace_id,
            safe_mode=safe_mode,
            framer_factory=framer_factory,
        )

    return _factory


class _LazyGardenPromoter:
    """Adapter that builds the canonicalization engine on first ``promote``.

    The engine (storage + index rebuild + service) is constructed inside the
    coroutine so the heavy canonicalization imports stay lazy (the worker module
    must import cheaply) and the per-workspace index is freshly rebuilt from the
    just-written vault state each pass — keeping promotion idempotent.
    """

    __slots__ = ("_vault_root", "_region", "_workspace_id", "_safe_mode", "_framer_factory")

    def __init__(
        self,
        *,
        vault_root: Path,
        region: str,
        workspace_id: uuid.UUID,
        safe_mode: bool,
        framer_factory: ConceptFramerFactory | None = None,
    ) -> None:
        self._vault_root = vault_root
        self._region = region
        self._workspace_id = workspace_id
        self._safe_mode = safe_mode
        self._framer_factory = framer_factory

    async def promote(self) -> object:
        # Lazy heavy imports — keep the worker entrypoint cheap.
        from backend.knowledge.canonicalization.decisions import DecisionMemory  # noqa: PLC0415
        from backend.knowledge.canonicalization.index import (  # noqa: PLC0415
            InMemoryCanonicalizationIndex,
        )
        from backend.knowledge.canonicalization.lock import AsyncIOMutationLock  # noqa: PLC0415
        from backend.knowledge.canonicalization.policies import PolicyResolver  # noqa: PLC0415
        from backend.knowledge.canonicalization.promotion import (  # noqa: PLC0415
            GardenObservationPromoter,
        )
        from backend.knowledge.canonicalization.resolver import TagResolver  # noqa: PLC0415
        from backend.knowledge.canonicalization.service import (  # noqa: PLC0415
            CanonicalizationService,
        )
        from backend.knowledge.canonicalization.store import NoteStore  # noqa: PLC0415
        from backend.knowledge.graph.storage import FileSystemStorage  # noqa: PLC0415

        # SAME boundary the sink wrote to: <vault_root>/<region>/<workspace_id>/.
        ws_root = self._vault_root / self._region / str(self._workspace_id)
        ws_root.mkdir(parents=True, exist_ok=True)
        storage = FileSystemStorage(ws_root)

        index = InMemoryCanonicalizationIndex()
        await index.initialize(storage)
        store = NoteStore(storage)
        # Wire the policy + decision memory so the service has a risk SIGNAL: the
        # scorer is built only when both are present (Class_Diagram §5). Without
        # them the Safe Mode gate has nothing to score and conservatively queues
        # EVERY action — which is exactly why a Safe-Mode workspace's knowledge
        # never settled (the trust loop treated 'add knowledge' as risk). With
        # the policy wired, the gate auto-applies low-risk anchors and queues
        # only genuine risk. bootstrap_defaults is idempotent (skips existing).
        policies = PolicyResolver(index=index, store=store)
        await policies.bootstrap_defaults()
        decisions = DecisionMemory(index=index, store=store)
        safe_mode = self._safe_mode
        service = CanonicalizationService(
            store=store,
            lock=AsyncIOMutationLock(),
            index=index,
            resolver=TagResolver(index=index),
            decisions=decisions,
            policies=policies,
            safe_mode=lambda: safe_mode,
        )
        # Lift 1b — resolve the routed concept framer for this workspace (None
        # when no factory is wired OR the workspace routed no canonicalization
        # model → deterministic Lift 1 body). Soft-fail: a resolution error
        # never blocks promotion.
        framer = None
        if self._framer_factory is not None:
            try:
                framer = await self._framer_factory(
                    region=self._region, workspace_id=self._workspace_id
                )
            except Exception:  # noqa: BLE001 — framing is derived; never break promotion
                logger.warning(
                    "concept_framer_resolution_failed",
                    workspace_id=str(self._workspace_id),
                    exc_info=True,
                )
        return await GardenObservationPromoter(service, framer=framer).promote()


class SettleWorker(BaseWorker):
    """Periodic drain of ``settle`` activities into BSage (the §4 write subscriber)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sink: SettleSink,
        config: SettleWorkerConfig | None = None,
        promoter_factory: PromoterFactory | None = None,
        embed_hook: NoteEmbedHook | None = None,
        reconcile_hook: ReconcileHook | None = None,
    ) -> None:
        self._cfg = config or SettleWorkerConfig()
        super().__init__(name="settle_worker", poll_interval_s=self._cfg.poll_interval_s)
        self._session_factory = session_factory
        self._sink = sink
        # When unset, promotion is disabled (e.g. tests exercising only the
        # drain bookkeeping). The runtime entrypoint wires the default factory.
        self._promoter_factory = promoter_factory
        # G5b: optional hook to populate the pgvector note store from a freshly
        # absorbed note. Soft-fail + independent session (see NoteEmbedHook) so a
        # missing/failed embedding never affects the settle SoT. Unset → no-op
        # (tests + workspaces with no embedding model configured).
        self._embed_hook = embed_hook
        # Lift 2: optional post-promotion embedding reconcile. Invoked once per
        # affected workspace whose promote pass CREATED an active concept, so the
        # new concept body (Lift 1) becomes retrievable without waiting for a
        # manual/cron reconcile. Soft-fail + own session (see ReconcileHook).
        # Unset → no-op (tests + workspaces with no embedding model configured).
        self._reconcile_hook = reconcile_hook

    async def _tick(self) -> int:
        return await self.drain_once()

    async def drain_once(self) -> int:
        """Absorb a batch of un-drained ``settle`` activities, then promote.

        Returns the count of activities absorbed. After the batch, the
        canonical-pattern promoter runs once per *affected* workspace (those
        with ≥1 successful absorb this batch) to close the §5 ratchet loop.
        Promotion is soft-fail: an error is logged and skipped, never reverting
        a settle write or affecting the returned drain count.
        """
        async with self._session_factory() as session:
            rows = await self._claim_undrained(session)
            if not rows:
                return 0
            policies = await self._resolve_workspaces(session, {r.workspace_id for r in rows})

            processed = 0
            promoted_ids: set[uuid.UUID] = set()
            for row in rows:
                policy = policies.get(
                    row.workspace_id, _WorkspacePolicy(self._cfg.default_region, True)
                )
                settlement = _to_settlement(row, policy.region)
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
                promoted_ids.add(row.workspace_id)
                logger.info(
                    "settle_worker_absorbed",
                    activity_id=str(row.id),
                    workspace_id=str(row.workspace_id),
                    node_ref=node_ref,
                )
                # G5b — populate the pgvector note store (its own session +
                # commit). Soft-fail: a missing/failed embedding never reverts
                # the absorbed+drained note above. No-op when no hook is wired.
                if self._embed_hook is not None and node_ref:
                    try:
                        await self._embed_hook(settlement, node_ref)
                    except Exception:  # noqa: BLE001 — embedding is derived; never break the drain
                        logger.warning(
                            "settle_worker_embed_failed",
                            activity_id=str(row.id),
                            workspace_id=str(row.workspace_id),
                            exc_info=True,
                        )

            # Close the loop: promote each affected workspace's accumulated
            # observations into canon. Derived + soft-fail — never affects the
            # drain count or reverts a write above.
            await self._promote_affected(promoted_ids, policies)
            return processed

    async def _promote_affected(
        self,
        workspace_ids: set[uuid.UUID],
        policies: dict[uuid.UUID, _WorkspacePolicy],
    ) -> None:
        """Run the canonical-pattern promoter for each affected workspace.

        Per-workspace, idempotent, policy-honoring, and soft-fail: each
        promotion is isolated so one workspace's failure can't stop another's,
        and no promotion error reverts the settlement writes (the SoT).

        Lift J — wraps each per-workspace promote in
        :func:`~backend.workflow.infrastructure.lease.try_workspace_promote_lock`.
        Two servers draining settle activities for the same workspace see one
        acquire + one ``busy`` skip (the skip is a no-op, NOT a failure — the
        OTHER server is already promoting; idempotent semantics make a single
        run sufficient). Acquires a fresh session per workspace so the lock
        scope is tightly bounded; the lock releases on session close even if
        the explicit ``release`` is skipped due to an unexpected exit.
        """
        if self._promoter_factory is None:
            return
        for workspace_id in sorted(workspace_ids, key=str):
            policy = policies.get(workspace_id, _WorkspacePolicy(self._cfg.default_region, True))
            async with self._session_factory() as lease_session:
                acquired = await try_workspace_promote_lock(lease_session, workspace_id)
                if not acquired:
                    logger.info(
                        "settle_worker_promotion_skipped_busy",
                        workspace_id=str(workspace_id),
                    )
                    continue
                try:
                    promoter = self._promoter_factory(
                        region=policy.region,
                        workspace_id=workspace_id,
                        safe_mode=policy.safe_mode,
                    )
                    if promoter is None:
                        continue
                    result = await promoter.promote()
                except Exception:  # noqa: BLE001 — promotion is derived; never break the drain
                    logger.exception(
                        "settle_worker_promotion_failed",
                        workspace_id=str(workspace_id),
                        safe_mode=policy.safe_mode,
                    )
                    continue
                else:
                    logger.info(
                        "settle_worker_promotion_complete",
                        workspace_id=str(workspace_id),
                        safe_mode=policy.safe_mode,
                    )
                    # Lift 2: a promote that CREATED an active concept produced
                    # fresh body the settle runtime won't embed on its own.
                    # Reconcile only then — the no-new-concept pass (the common
                    # case, and every Safe-Mode pass) stays a cheap no-op rather
                    # than a full vault scan. Soft-fail: never reverts the
                    # promotion the drain just logged complete.
                    await self._reconcile_after_promotion(result, policy.region, workspace_id)
                finally:
                    await release_workspace_promote_lock(lease_session, workspace_id)

    async def _reconcile_after_promotion(
        self, result: object, region: str, workspace_id: uuid.UUID
    ) -> None:
        """Embed freshly created concepts. Gated + soft-fail (Lift 2)."""
        if self._reconcile_hook is None:
            return
        if not getattr(result, "created_concepts", None):
            return
        try:
            await self._reconcile_hook(region=region, workspace_id=workspace_id)
        except Exception:  # noqa: BLE001 — reconcile is derived; never break the drain
            logger.exception(
                "settle_worker_reconcile_failed",
                workspace_id=str(workspace_id),
            )
        else:
            logger.info(
                "settle_worker_reconcile_complete",
                workspace_id=str(workspace_id),
            )

    async def _claim_undrained(self, session: AsyncSession) -> list[ExecutionRunActivity]:
        stmt = build_settle_claim_stmt(batch_size=self._cfg.batch_size)
        return list((await session.execute(stmt)).scalars().all())

    async def _resolve_workspaces(
        self, session: AsyncSession, workspace_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, _WorkspacePolicy]:
        """Resolve region + canonicalization policy for each workspace.

        A missing row (telemetry can outlive its workspace) falls back to the
        configured default region + the strict Safe-Mode default — never
        auto-applying canon for an unknown workspace.
        """
        ids = list(workspace_ids)
        if not ids:
            return {}
        stmt = select(WorkspaceRow.id, WorkspaceRow.region, WorkspaceRow.safe_mode).where(
            WorkspaceRow.id.in_(ids)
        )
        return {
            wid: _WorkspacePolicy(region, bool(safe_mode))
            for wid, region, safe_mode in (await session.execute(stmt)).all()
        }


def build_settle_claim_stmt(*, batch_size: int) -> Select[tuple[ExecutionRunActivity]]:
    """Lift J — multi-server safe claim of un-drained settle activities.

    ``FOR UPDATE SKIP LOCKED`` makes the SELECT atomic w.r.t. a second
    server: each worker's claim sees a disjoint set of rows. The
    ``NOT EXISTS`` filter against :class:`SettleDrainRow` is the existing
    at-least-once idempotence; the lock hint is the layer that prevents
    two workers from racing into ``sink.absorb`` for the same activity
    in the gap between SELECT and the per-row marker commit.

    Extracted as a builder so the unit test pins the rendered SQL
    carries ``FOR UPDATE SKIP LOCKED``.
    """
    already_drained = select(SettleDrainRow.activity_id)
    return (
        select(ExecutionRunActivity)
        .where(
            ExecutionRunActivity.activity_type == SETTLE_ACTIVITY_TYPE,
            ExecutionRunActivity.id.notin_(already_drained),
        )
        .order_by(ExecutionRunActivity.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


def _opt_str(value: object) -> str | None:
    """Coerce a settle-payload value to a non-empty string, else ``None``.

    The settle activity carries product/intent as JSON; an absent key (connector
    run) or an empty/blank value degrades to ``None`` so derivation falls back to
    the PR #27 summary + artifact-refs behaviour.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


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
        product_slug=_opt_str(payload.get("product_slug")),
        product_name=_opt_str(payload.get("product_name")),
        intent_text=_opt_str(payload.get("intent_text")),
        occurred_at=row.created_at,
        # B11b — decision-resolution metadata when this settle row came from
        # the checkpoints resolve endpoint; ``None`` for verified-work settles.
        kind=_opt_str(payload.get("kind")),
        question=_opt_str(payload.get("question")),
        answer=_opt_str(payload.get("answer")),
        # G1 — rejection reason when this settle row came from a discard-with-
        # reason; ``None`` for every other settlement kind.
        reason=_opt_str(payload.get("reason")),
    )


__all__ = [
    "ConceptFramerFactory",
    "EntityExtractor",
    "ExtractorFactory",
    "KnowledgeSettleSink",
    "MemoryExtractor",
    "MemoryExtractorFactory",
    "NoteEmbedHook",
    "PromoterFactory",
    "ReconcileHook",
    "SettleSink",
    "SettleWorker",
    "SettleWorkerConfig",
    "Settlement",
    "WorkspacePromoter",
    "build_garden_promoter_factory",
    "derive_content_tags",
]
