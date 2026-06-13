"""GardenObservationPromoter — promote garden/observation patterns into canon.

The trust-ratchet learning loop deposits each verified work step as a BSage
**garden observation** note (see :class:`backend.knowledge.infrastructure.workers.settle_worker.KnowledgeSettleSink`).
Those observations accumulate forever, but until promotion runs they are inert:
the tags they carry (the recurring entity names / patterns) never become
**canonical anchors**, so deterministic retrieval can't collapse variant
spellings onto one node — the "wall" behaviour is missing.

This module is the knowledge-layer **promotion entry point** that closes that
gap. It drives the *existing* canonicalization engine over a workspace's
garden observation notes — it does NOT reimplement scoring, the proposal /
decision state machine, or policy semantics:

1. **Collect candidate names** — read garden notes (``garden/<maturity>/...``)
   and gather their content ``tags``, dropping structural tags (``settle``,
   ``verified-run``, ...) that describe the *kind* of note rather than what it
   is *about* (Handoff §0.2: tags are content, not kind), then keep only tags
   that **recur** across ``>= _MIN_OBSERVATIONS_FOR_PROMOTION`` observations
   (BSage's recurrence mechanism — suppresses one-off noise without a deny-list).

2. **Seed candidate concepts** — for every candidate tag without an active
   concept, route a ``create-concept`` action through
   :class:`~backend.knowledge.canonicalization.service.CanonicalizationService`.
   This already honours Safe Mode: under the strict default policy the action
   is persisted ``pending_approval`` (queued for review); only an explicitly
   permissive policy auto-applies it into an active concept (the vault-SoT
   canonical anchor).

3. **Cluster variants → merge proposals** — run the existing
   :class:`~backend.knowledge.canonicalization.proposals.DeterministicProposer`
   over the active concepts. It clusters lexically-similar concept ids
   (character-trigram Jaccard — purely deterministic, no LLM/network) and emits
   ``merge-concepts`` proposals, each linked to a queued draft merge action.

4. **Policy gate** — under permissive policy the promoter ``accept_proposal``s
   each merge proposal, applying the merge so variants fold into one canonical
   concept (the survivor's id), with the merged ids recorded as aliases. Under
   the Safe-Mode default the proposals + their draft actions stay **queued**;
   nothing is silently applied.

5. **Deterministic retrieval** — once a concept (and, after merge, its aliases)
   exists, :class:`~backend.knowledge.canonicalization.resolver.TagResolver`
   resolves every variant spelling to the canonical id.

Idempotency: re-running is a no-op for already-promoted patterns — the
resolver dedups concept creation (a seen tag resolves instead of re-drafting)
and the proposer dedups merge proposals by ``(canonical, merge-list)``
signature.

Per-workspace scoping is structural: the caller constructs the engine over a
``StorageBackend`` rooted at ``<vault_root>/<region>/<workspace_id>/`` (see
:class:`backend.knowledge.factory.KnowledgeFactory`), so a promoter can never
read or write another workspace's vault.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.proposals import DeterministicProposer
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore

logger = structlog.get_logger(__name__)

# Structural tags written by the settle/garden write path that describe the
# *kind* of note, not what it is *about*. They must never become canonical
# anchors (Handoff §0.2 — path/frontmatter/tag jobs are distinct). The settle
# sink stamps ``settle`` + ``verified-run`` on every observation; add more
# here as other producers introduce their own structural markers.
_DEFAULT_STRUCTURAL_TAGS: frozenset[str] = frozenset({"settle", "verified-run"})

# Recurrence gate (complementary safeguard — mirrors BSage's ``evolution_config``
# ``edge_promotion_min_mentions`` / ``promotion_frequency_ratio``): a tag only
# becomes a candidate concept after it recurs across at least this many distinct
# garden observations. One-off noise (a single odd entity from one run) never
# anchors a concept; a pattern the work keeps coming back to does. Kept small so
# a genuinely recurring subject is promoted promptly.
_MIN_OBSERVATIONS_FOR_PROMOTION = 2


@dataclass(slots=True)
class PromotionResult:
    """Outcome of one :meth:`GardenObservationPromoter.promote` pass.

    Both policy modes populate ``candidate_tags`` and ``proposals``; only the
    permissive mode populates ``applied_anchors`` / ``applied_merges``. The
    Safe-Mode default leaves ``pending_actions`` (queued create-concept and
    merge draft actions) non-empty with nothing applied.
    """

    candidate_tags: list[str] = field(default_factory=list)
    created_concepts: list[str] = field(default_factory=list)
    pending_actions: list[str] = field(default_factory=list)
    proposals: list[str] = field(default_factory=list)
    applied_anchors: list[str] = field(default_factory=list)
    applied_merges: list[str] = field(default_factory=list)


class GardenObservationPromoter:
    """Promote recurring garden-observation patterns into canonical anchors.

    Uses the *existing* :class:`CanonicalizationService` (seeding +
    apply/accept), :class:`DeterministicProposer` (clustering), and
    :class:`TagResolver` (deterministic retrieval). The promoter adds no new
    scoring or data-model surface — it only orchestrates them over garden
    observation notes.
    """

    def __init__(
        self,
        service: CanonicalizationService,
        *,
        proposer: DeterministicProposer | None = None,
        structural_tags: frozenset[str] = _DEFAULT_STRUCTURAL_TAGS,
        actor: str = "promotion",
    ) -> None:
        self._service = service
        self._store: NoteStore = service._store
        index = service._index
        if index is None:
            msg = "GardenObservationPromoter requires a service with an index wired"
            raise ValueError(msg)
        self._index: CanonicalizationIndex = index
        self._resolver: TagResolver = service._resolver or TagResolver(index=index)
        # Reuse the service clock so seeded concepts / proposals share one
        # deterministic timeline in tests.
        self._proposer = proposer or DeterministicProposer(
            index=index,
            store=self._store,
            clock=service._clock,
        )
        self._structural_tags = structural_tags
        self._actor = actor

    async def promote(self) -> PromotionResult:
        """Run one promotion pass over the workspace's garden observations.

        Steps (each delegating to the existing engine):

        1. Collect candidate tags from garden observation notes.
        2. Seed a ``create-concept`` per unseen candidate tag (Safe Mode aware).
        3. Run the proposer to cluster variant concept ids into ``merge``
           proposals.
        4. Under permissive policy, accept each proposal (apply the merge);
           under Safe Mode leave them queued.
        """
        result = PromotionResult()

        candidate_tags = await self._collect_candidate_tags()
        result.candidate_tags = candidate_tags

        # 1+2. Seed candidate concepts. ``resolve_and_canonicalize`` is the
        # spec's tag → concept hook (Handoff §11): it resolves existing tags
        # (idempotent) and routes a create-concept draft through the service
        # for new ones, where Safe Mode decides queue vs auto-apply.
        for tag in candidate_tags:
            await self._seed_concept(tag, result)

        # 3. Cluster variant concept ids into merge-concepts proposals. The
        # proposer writes a paired draft merge action per proposal and dedups
        # by (canonical, merge) signature, so re-runs add nothing.
        proposals = await self._proposer.generate()
        result.proposals = list(proposals)

        # 4. Policy gate: under permissive policy, accepting a proposal applies
        # its linked merge action (variants → canonical aliases). Under the
        # Safe-Mode default the linked action persists ``pending_approval`` and
        # ``accept_proposal`` leaves the proposal ``pending`` — nothing is
        # silently applied.
        for proposal_path in proposals:
            await self._maybe_accept(proposal_path, result)

        logger.info(
            "garden_observation_promotion_complete",
            candidate_tags=len(result.candidate_tags),
            created_concepts=len(result.created_concepts),
            pending_actions=len(result.pending_actions),
            proposals=len(result.proposals),
            applied_anchors=len(result.applied_anchors),
            applied_merges=len(result.applied_merges),
        )
        return result

    # ------------------------------------------------------------- internals

    async def _collect_candidate_tags(self) -> list[str]:
        """Gather recurring content tags across garden observation notes.

        Drops structural tags (``settle`` / ``verified-run`` / ...) and any tag
        that does not normalize to a valid concept id, then applies the
        **recurrence gate**: a tag is only a candidate after it appears in
        ``>= _MIN_OBSERVATIONS_FOR_PROMOTION`` distinct observations. Recurrence
        is counted on the *normalized* form so variant spellings of one concept
        accumulate together; the first-seen raw spelling is returned as the
        representative. This replaces the old open-ended filler deny-list:
        concepts now come from LLM-extracted entities (the settle sink), and a
        recurrence threshold (BSage's ``evolution_config`` mechanism) suppresses
        any remaining one-off noise *structurally* rather than by enumerating
        non-concept words. Order is stable (sorted) so seeding + proposals are
        deterministic.
        """
        observation_counts: dict[str, int] = {}
        representative: dict[str, str] = {}
        # Lift E26 — track the seedling note kind (E20 type field) per
        # normalized tag so the promoter can stamp a dominant ``type:`` onto
        # the concept it creates. ``type_counts[normalized][type]`` =
        # observations. The promoter reads this via :attr:`_type_votes`.
        self._type_votes = {}  # type: dict[str, dict[str, int]]
        for path in await self._store.list_garden_paths():
            try:
                tags = await self._store.read_garden_tags(path)
            except FileNotFoundError:  # pragma: no cover — listing/read race
                continue
            # E26 — alongside the tags, read the seedling's ``type:``. Missing
            # type is treated as "no vote" (pre-E20 notes, retags, …).
            note_type = await self._store.read_garden_note_type(path)
            # Count each normalized tag at most ONCE per observation — recurrence
            # is across notes, not repeated tags within one note.
            in_this_note: set[str] = set()
            for raw in tags:
                if not isinstance(raw, str) or raw in self._structural_tags:
                    continue
                normalized = self._resolver.normalize(raw)
                if not normalized or normalized in self._structural_tags:
                    continue
                in_this_note.add(normalized)
                representative.setdefault(normalized, raw)
            for normalized in in_this_note:
                observation_counts[normalized] = observation_counts.get(normalized, 0) + 1
                if note_type:
                    bucket = self._type_votes.setdefault(normalized, {})
                    bucket[note_type] = bucket.get(note_type, 0) + 1

        survivors = {
            representative[normalized]
            for normalized, count in observation_counts.items()
            if count >= _MIN_OBSERVATIONS_FOR_PROMOTION
        }
        return sorted(survivors)

    def _dominant_type_for(self, raw_tag: str) -> str | None:
        """Lift E26 — pick the seedling type that voted most for this tag.

        Tie-break is the E20 declaration order so the picks are stable across
        runs (deterministic seed → deterministic concept type). Returns
        ``None`` when no typed seedling contributed (pre-E20 vault, retag-only
        tags, …) so the concept stays unmarked rather than being mistyped.
        """
        if not getattr(self, "_type_votes", None):
            return None
        normalized = self._resolver.normalize(raw_tag)
        if not normalized:
            return None
        votes = self._type_votes.get(normalized)
        if not votes:
            return None
        priority = {"Pattern": 0, "Principle": 1, "TechInsight": 2, "DomainModel": 3}
        return max(
            votes.items(),
            key=lambda kv: (kv[1], -priority.get(kv[0], 99)),
        )[0]

    async def _seed_concept(self, raw_tag: str, result: PromotionResult) -> None:
        """Ensure a candidate concept exists for ``raw_tag``.

        Delegates to ``resolve_and_canonicalize`` with ``auto_apply=False`` so
        the *service* — not this orchestrator — owns the Safe Mode decision:

        * already resolves (existing concept / alias) → no-op (idempotent).
        * permissive policy (Safe Mode off) → re-apply the just-created draft,
          producing an active concept and recording it as a created anchor.
        * Safe Mode default → the create-concept draft stays ``pending_approval``
          and is recorded under ``pending_actions``.
        """
        normalized = self._resolver.normalize(raw_tag)
        if not normalized:
            return

        # Idempotency fast-path: already an active concept (or alias of one).
        existing = await self._resolver.resolve(raw_tag)
        if existing.status == "resolved":
            return

        # Draft the create-concept (no auto-apply — keep the policy decision in
        # the service). resolve_and_canonicalize returns the draft path via the
        # pending/new candidate flow; we then drive apply through the service so
        # Safe Mode gating is honoured.
        # Lift E26 — pass the dominant seedling type so the create-concept
        # action carries it through to ``ConceptEntry.note_type`` and the
        # concept's frontmatter ``type:`` field.
        note_type = self._dominant_type_for(raw_tag)
        await self._service.resolve_and_canonicalize(
            raw_tag,
            raw_source="garden-observation",
            auto_apply=False,
            note_type=note_type,
        )

        draft = await self._index.find_pending_concept_draft(normalized)
        if draft is None:
            # ambiguous / blocked / deprecated — nothing to seed.
            return

        applied = await self._service.apply_action(draft.path, actor=self._actor)
        if applied.final_status == "applied":
            result.created_concepts.append(normalized)
        elif applied.final_status == "pending_approval":
            result.pending_actions.append(draft.path)

    async def _maybe_accept(self, proposal_path: str, result: PromotionResult) -> None:
        """Accept a merge proposal, letting the service apply or queue it.

        ``accept_proposal`` applies each linked draft action via
        ``apply_action`` — which re-checks Safe Mode. Under permissive policy
        the merge applies (proposal → ``accepted``); under Safe Mode the linked
        action becomes ``pending_approval`` and the proposal stays ``pending``.
        """
        proposal = await self._store.read_proposal(proposal_path)
        if proposal is None or proposal.status != "pending":
            return

        results = await self._service.accept_proposal(proposal_path, actor=self._actor)
        for apply_result in results:
            if apply_result.final_status == "applied":
                result.applied_merges.append(apply_result.action_path)
                # The survivor concept file is the canonical anchor in the
                # vault-SoT model — surface its path for the caller.
                result.applied_anchors.extend(
                    p for p in apply_result.affected_paths if p.startswith("concepts/active/")
                )
            elif apply_result.final_status == "pending_approval":
                result.pending_actions.append(apply_result.action_path)

        # Dedupe anchors while preserving order.
        result.applied_anchors = list(dict.fromkeys(result.applied_anchors))


__all__ = ["GardenObservationPromoter", "PromotionResult"]
