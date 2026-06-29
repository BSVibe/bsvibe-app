"""End-to-end tests for garden-observation → canonical-anchor promotion.

Proves the trust-ratchet promotion pipeline at the knowledge API level
(NOT wired into SettleWorker here — that binding is a separate follow-up):

    garden observation notes (variant tags)
        -> GardenObservationPromoter.promote()
            -> seed concepts (Safe Mode aware)
            -> DeterministicProposer clusters variants -> merge proposals
            -> policy gate: queued proposals (Safe Mode) | applied anchors (permissive)
        -> TagResolver resolves every variant to the canonical id (permissive)

No real LLM / network: the proposer is purely lexical (character-trigram
Jaccard). Per-workspace scoping is structural — the storage backend is rooted
at ``<vault_root>/<region>/<workspace_id>/`` exactly as KnowledgeFactory does.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.promotion import GardenObservationPromoter
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import body_after_frontmatter, extract_frontmatter
from backend.knowledge.graph.storage import FileSystemStorage

_FIXED_NOW = datetime(2026, 5, 23, 12, 0, 0)
_REGION = "us-1"
_WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def workspace_storage(tmp_path: Path) -> FileSystemStorage:
    """Storage rooted exactly like KnowledgeFactory's per-workspace vault."""
    vault_root = tmp_path / _REGION / _WORKSPACE_ID
    vault_root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(vault_root)


async def _make_service(storage: FileSystemStorage, *, safe_mode: bool) -> CanonicalizationService:
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: _FIXED_NOW,
        safe_mode=lambda: safe_mode,
    )


async def _seed_garden_observations(storage: FileSystemStorage) -> None:
    """Seed settle-style garden observation notes referencing one entity
    under two variant spellings (``self-hosting`` / ``self-host``) plus an
    unrelated entity, alongside the structural settle tags.

    Every entity recurs across **>= 2 distinct observations** so it clears the
    promoter's recurrence gate (``_MIN_OBSERVATIONS_FOR_PROMOTION``) — the gate
    suppresses one-off noise, so a candidate must be a genuinely recurring
    pattern, which is exactly what these fixtures represent."""
    # Four observations tagged with the dominant spelling ...
    for i in range(4):
        await storage.write(
            f"garden/seedling/settle-self-hosting-{i}.md",
            "---\n"
            "tags:\n"
            "  - settle\n"
            "  - verified-run\n"
            "  - self-hosting\n"
            "---\n"
            "# Settle: configured reverse proxy\n",
        )
    # ... and two with the variant spelling (recurs, so it clears the gate).
    for i in range(2):
        await storage.write(
            f"garden/seedling/settle-self-host-{i}.md",
            "---\n"
            "tags:\n"
            "  - settle\n"
            "  - verified-run\n"
            "  - self-host\n"
            "---\n"
            "# Settle: hardened self host\n",
        )
    # An unrelated entity (recurring) that must NOT cluster with the self-host* pair.
    for i in range(2):
        await storage.write(
            f"garden/seedling/settle-vaultwarden-{i}.md",
            "---\n"
            "tags:\n"
            "  - settle\n"
            "  - verified-run\n"
            "  - vaultwarden\n"
            "---\n"
            "# Settle: vaultwarden backup\n",
        )


class TestSafeModeDefault:
    """Default strict policy: queue-only — proposals/actions, never applied."""

    @pytest.mark.asyncio
    async def test_promotion_queues_proposals_does_not_auto_apply(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        service = await _make_service(workspace_storage, safe_mode=True)
        await _seed_garden_observations(workspace_storage)

        promoter = GardenObservationPromoter(service)
        result = await promoter.promote()

        # Candidate tags exclude structural settle markers.
        assert "settle" not in result.candidate_tags
        assert "verified-run" not in result.candidate_tags
        assert set(result.candidate_tags) == {"self-hosting", "self-host", "vaultwarden"}

        # Safe Mode: create-concept actions are QUEUED, not applied — so no
        # active concept files exist yet.
        assert result.created_concepts == []
        assert await workspace_storage.list_files("concepts/active") == []
        assert result.applied_anchors == []
        assert result.applied_merges == []

        # The create-concept drafts are pending_approval (queued for review).
        assert len(result.pending_actions) >= 3
        create_actions = await workspace_storage.list_files("actions/create-concept")
        assert len(create_actions) == 3
        for action_path in create_actions:
            fm = extract_frontmatter(await workspace_storage.read(action_path))
            assert fm["status"] == "pending_approval"

        # No merge proposal yet — concepts aren't active so the proposer has
        # nothing to cluster. The pattern stays entirely in the review queue.
        assert result.proposals == []

    @pytest.mark.asyncio
    async def test_safe_mode_proposals_queued_when_concepts_exist(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        """When active concepts already exist, Safe Mode still queues the merge
        action (proposal stays pending, nothing merged)."""
        # First, create the variant concepts under a permissive service so they
        # are active (simulating prior approval).
        permissive = await _make_service(workspace_storage, safe_mode=False)
        for cid in ("self-hosting", "self-host"):
            draft = await permissive.create_action_draft(
                kind="create-concept", params={"concept": cid, "title": cid}
            )
            await permissive.apply_action(draft, actor="test")
        await _seed_garden_observations(workspace_storage)

        # Now promote under Safe Mode.
        service = await _make_service(workspace_storage, safe_mode=True)
        promoter = GardenObservationPromoter(service)
        result = await promoter.promote()

        # A merge proposal is generated (clustering is policy-independent) ...
        assert len(result.proposals) == 1
        # ... but accepting it queues the merge action rather than applying it.
        assert result.applied_merges == []
        assert len(result.pending_actions) >= 1
        proposal_fm = extract_frontmatter(await workspace_storage.read(result.proposals[0]))
        assert proposal_fm["status"] == "pending"
        merge_action = proposal_fm["action_drafts"][0]
        action_fm = extract_frontmatter(await workspace_storage.read(merge_action))
        assert action_fm["status"] == "pending_approval"
        # Both variant concepts remain independent (no merge applied).
        assert await workspace_storage.exists("concepts/active/self-hosting.md")
        assert await workspace_storage.exists("concepts/active/self-host.md")


class TestPermissivePolicy:
    """Permissive policy (Safe Mode off): anchors created, variants merged,
    resolver collapses variants onto the canonical id."""

    @pytest.mark.asyncio
    async def test_promotion_creates_anchor_and_resolver_collapses_variants(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        service = await _make_service(workspace_storage, safe_mode=False)
        await _seed_garden_observations(workspace_storage)

        promoter = GardenObservationPromoter(service)
        result = await promoter.promote()

        # Concepts auto-created for each candidate tag (the canonical anchors).
        assert set(result.created_concepts) == {"self-hosting", "self-host", "vaultwarden"}
        assert await workspace_storage.exists("concepts/active/vaultwarden.md")

        # The variant pair clustered into exactly one merge proposal, accepted.
        assert len(result.proposals) == 1
        assert len(result.applied_merges) == 1
        proposal_fm = extract_frontmatter(await workspace_storage.read(result.proposals[0]))
        assert proposal_fm["status"] == "accepted"

        # Exactly one of the variant pair survives as the canonical concept;
        # the other becomes a tombstone redirecting to it.
        survivors = {
            p.removeprefix("concepts/active/").removesuffix(".md")
            for p in await workspace_storage.list_files("concepts/active")
        }
        # vaultwarden untouched + one survivor of the self-host* pair.
        assert "vaultwarden" in survivors
        self_host_survivors = survivors & {"self-hosting", "self-host"}
        assert len(self_host_survivors) == 1
        canonical = next(iter(self_host_survivors))
        merged = ({"self-hosting", "self-host"} - self_host_survivors).pop()
        assert await workspace_storage.exists(f"concepts/merged/{merged}.md")

        # Deterministic retrieval: BOTH variant spellings now resolve to the
        # canonical id (survivor via direct id, merged via tombstone redirect /
        # alias).
        resolver = TagResolver(index=service._index)
        for variant in ("self-hosting", "self-host", "Self Hosting", "self_host"):
            resolved = await resolver.resolve(variant)
            assert resolved.status == "resolved", (variant, resolved)
            assert resolved.concept_id == canonical, (variant, resolved.concept_id)

        # Unrelated entity still resolves to itself, never folded into the pair.
        vault_resolved = await resolver.resolve("vaultwarden")
        assert vault_resolved.concept_id == "vaultwarden"


class TestIdempotency:
    """Re-running promotion produces no duplicate concepts, proposals, or
    merges — the resolver and proposer both dedup."""

    @pytest.mark.asyncio
    async def test_second_run_is_noop(self, workspace_storage: FileSystemStorage) -> None:
        service = await _make_service(workspace_storage, safe_mode=False)
        await _seed_garden_observations(workspace_storage)
        promoter = GardenObservationPromoter(service)

        first = await promoter.promote()
        assert len(first.created_concepts) == 3
        assert len(first.applied_merges) == 1

        active_after_first = sorted(await workspace_storage.list_files("concepts/active"))
        proposals_after_first = sorted(
            await workspace_storage.list_files("proposals/merge-concepts")
        )

        second = await promoter.promote()
        # Nothing new created/applied on the second pass.
        assert second.created_concepts == []
        assert second.applied_merges == []
        assert second.proposals == []

        # Vault state unchanged.
        assert sorted(await workspace_storage.list_files("concepts/active")) == active_after_first
        assert (
            sorted(await workspace_storage.list_files("proposals/merge-concepts"))
            == proposals_after_first
        )


class TestEmptyVault:
    @pytest.mark.asyncio
    async def test_no_garden_notes_returns_empty(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        service = await _make_service(workspace_storage, safe_mode=False)
        result = await GardenObservationPromoter(service).promote()
        assert result.candidate_tags == []
        assert result.created_concepts == []
        assert result.proposals == []


class TestE26TypePropagation:
    """Lift E26 — the dominant seedling ``type:`` (E20 whitelist) is carried
    through promotion onto the concept's frontmatter so the founder can
    distinguish a Pattern from a Principle / DomainModel / TechInsight when
    browsing ``concepts/active/``."""

    @pytest.mark.asyncio
    async def test_promoted_concept_inherits_seedling_type(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        # Seed two observations all stamped ``type: Pattern`` referencing
        # one entity — promotion's recurrence gate fires and the concept
        # is created.
        for i in range(2):
            await workspace_storage.write(
                f"garden/seedling/observation-{i}.md",
                "---\n"
                "type: Pattern\n"
                "tags:\n"
                "  - settle\n"
                "  - oauth-loopback\n"
                "---\n"
                "# OAuth loopback observation\n",
            )

        service = await _make_service(workspace_storage, safe_mode=False)
        await GardenObservationPromoter(service).promote()

        fm = extract_frontmatter(await workspace_storage.read("concepts/active/oauth-loopback.md"))
        assert fm.get("type") == "Pattern"

    @pytest.mark.asyncio
    async def test_majority_type_wins(self, workspace_storage: FileSystemStorage) -> None:
        # 3 Pattern + 1 Principle for the same tag → Pattern wins.
        for i in range(3):
            await workspace_storage.write(
                f"garden/seedling/pat-{i}.md",
                "---\ntype: Pattern\ntags:\n  - settle\n  - async-cancel\n---\n# Pattern note\n",
            )
        await workspace_storage.write(
            "garden/seedling/principle-0.md",
            "---\ntype: Principle\ntags:\n  - settle\n  - async-cancel\n---\n# Principle note\n",
        )

        service = await _make_service(workspace_storage, safe_mode=False)
        await GardenObservationPromoter(service).promote()

        fm = extract_frontmatter(await workspace_storage.read("concepts/active/async-cancel.md"))
        assert fm.get("type") == "Pattern"

    @pytest.mark.asyncio
    async def test_untyped_seedlings_leave_concept_unmarked(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        # Pre-E20 / legacy notes have no ``type:``. The promoted concept
        # must stay unmarked (no ``type:`` frontmatter field) rather than
        # be assigned a guessed kind.
        for i in range(2):
            await workspace_storage.write(
                f"garden/seedling/legacy-{i}.md",
                "---\ntags:\n  - settle\n  - legacy-tag\n---\n# Legacy note\n",
            )

        service = await _make_service(workspace_storage, safe_mode=False)
        await GardenObservationPromoter(service).promote()

        fm = extract_frontmatter(await workspace_storage.read("concepts/active/legacy-tag.md"))
        assert "type" not in fm

    @pytest.mark.asyncio
    async def test_only_structural_tags_yields_no_candidates(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        await workspace_storage.write(
            "garden/seedling/empty.md",
            "---\ntags:\n  - settle\n  - verified-run\n---\n# Settle: nothing notable\n",
        )
        service = await _make_service(workspace_storage, safe_mode=False)
        result = await GardenObservationPromoter(service).promote()
        assert result.candidate_tags == []
        assert result.created_concepts == []


class TestRecurrenceGate:
    """Recurrence gate replaces the retired ``filler_words`` deny-list.

    Concepts now come from LLM-extracted entities at the settle sink (generic
    nouns are excluded *structurally*), and the promoter only promotes a tag
    that recurs across ``>= _MIN_OBSERVATIONS_FOR_PROMOTION`` distinct
    observations. A one-off tag (whether a genuine subject seen once or stray
    noise) is NOT promoted; a tag the work keeps coming back to IS — without any
    enumerated list of non-concept words.
    """

    async def _seed_once_and_twice(self, storage: FileSystemStorage) -> None:
        """``recurring`` appears in two observations; ``oneoff`` in only one."""
        for i in range(2):
            await storage.write(
                f"garden/seedling/obs-recurring-{i}.md",
                "---\ntags:\n  - settle\n  - verified-run\n  - recurring\n  - calculator\n---\n# o\n",
            )
        await storage.write(
            "garden/seedling/obs-oneoff.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - oneoff\n---\n# o\n",
        )

    @pytest.mark.asyncio
    async def test_once_seen_tag_not_promoted_recurring_is(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        service = await _make_service(workspace_storage, safe_mode=False)
        await self._seed_once_and_twice(workspace_storage)

        result = await GardenObservationPromoter(service).promote()

        # Recurring tags (>= 2 observations) are candidates + active anchors.
        recurring = {"recurring", "calculator"}
        assert recurring <= set(result.candidate_tags), result.candidate_tags
        assert recurring <= set(result.created_concepts), result.created_concepts
        # A tag seen in only ONE observation is suppressed by the gate.
        assert "oneoff" not in result.candidate_tags
        assert "oneoff" not in result.created_concepts
        active = {
            p.removeprefix("concepts/active/").removesuffix(".md")
            for p in await workspace_storage.list_files("concepts/active")
        }
        assert recurring <= active, active
        assert "oneoff" not in active

    @pytest.mark.asyncio
    async def test_variant_spellings_count_toward_recurrence_together(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        """Recurrence is counted on the NORMALIZED form, so the e2e vocabulary
        (self-host*, vaultwarden, auth, client, the product slug) — each seeded
        across >= 2 observations — all survives the gate."""
        service = await _make_service(workspace_storage, safe_mode=False)
        await _seed_garden_observations(workspace_storage)
        for i in range(2):
            await workspace_storage.write(
                f"garden/seedling/obs-extra-{i}.md",
                "---\ntags:\n  - settle\n  - auth\n  - client\n"
                "  - vaultwarden-selfhost\n---\n# obs\n",
            )

        result = await GardenObservationPromoter(service).promote()

        survivors = {
            "self-hosting",
            "self-host",
            "vaultwarden",
            "auth",
            "client",
            "vaultwarden-selfhost",
        }
        assert survivors <= set(result.candidate_tags), result.candidate_tags


class TestPromoterRequiresIndex:
    @pytest.mark.asyncio
    async def test_service_without_index_rejected(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        service = CanonicalizationService(
            store=NoteStore(workspace_storage),
            lock=AsyncIOMutationLock(),
        )
        with pytest.raises(ValueError, match="index"):
            GardenObservationPromoter(service)


class TestConceptSynthesisBody:
    """KG Lift 1 — a promoted concept is a SUBSTANTIVE hub: its body carries the
    member garden seedlings as ``[[wikilinks]]`` with their excerpts (a MOC),
    not an empty ``# Title`` shell."""

    @pytest.mark.asyncio
    async def test_promoted_concept_carries_member_synthesis_body(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        # Two recurring observations (clears the gate) WITH real body content.
        for i in range(2):
            await workspace_storage.write(
                f"garden/seedling/resolver-soft-fallback-{i}.md",
                "---\n"
                "tags:\n"
                "  - settle\n"
                "  - resolver-pattern\n"
                "---\n"
                f"# Resolver soft fallback {i}\n\n"
                "Resolvers should return None on a miss so callers degrade.\n",
            )
        service = await _make_service(workspace_storage, safe_mode=False)
        promoter = GardenObservationPromoter(service)

        await promoter.promote()

        concept_paths = await workspace_storage.list_files("concepts/active")
        match = [p for p in concept_paths if "resolver-pattern" in p]
        assert match, f"expected a resolver-pattern concept, got {concept_paths}"
        body = await workspace_storage.read(match[0])

        # Links to the member seedlings (explicit wikilinks → Lift 5 graph edges).
        assert "[[resolver-soft-fallback-0]]" in body
        # The member's substance is carried (the excerpt), so the concept is not
        # a hollow ``# Title`` anymore.
        assert "Resolvers should return None on a miss" in body
        # And the body after the heading is non-trivial (not just the title line).
        after_heading = body_after_frontmatter(body).split("\n", 1)[1].strip()
        assert len(after_heading) > 20

    @pytest.mark.asyncio
    async def test_framer_distills_synthesis_paragraph_above_the_moc(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        """KG Lift 1b — when a ConceptFramer is wired (model resolved via the
        user's routing), the concept body LEADS with a distilled 2-4 sentence
        synthesis framing, THEN the deterministic member [[wikilink]] MOC. The
        framer sees the concept + its member summaries (deterministic inputs)."""
        for i in range(2):
            await workspace_storage.write(
                f"garden/seedling/resolver-soft-fallback-{i}.md",
                "---\ntags:\n  - settle\n  - resolver-pattern\n---\n"
                f"# Resolver soft fallback {i}\n\n"
                "Resolvers should return None on a miss so callers degrade.\n",
            )

        seen: dict[str, object] = {}

        class _Framer:
            async def frame(self, *, concept: str, members: list[tuple[str, str]]) -> str | None:
                seen["concept"] = concept
                seen["members"] = members
                return "Resolvers degrade rather than crash on a miss."

        service = await _make_service(workspace_storage, safe_mode=False)
        await GardenObservationPromoter(service, framer=_Framer()).promote()

        match = [
            p
            for p in await workspace_storage.list_files("concepts/active")
            if "resolver-pattern" in p
        ]
        assert match, "expected a resolver-pattern concept"
        body = await workspace_storage.read(match[0])

        # The distilled framing leads the body, ABOVE the deterministic MOC list.
        assert "Resolvers degrade rather than crash on a miss." in body
        framing_at = body.index("Resolvers degrade rather than crash")
        moc_at = body.index("Synthesized from")
        assert framing_at < moc_at, "framing must lead, MOC list follows"
        # The MOC links survive (framing augments, never replaces, Lift 1).
        assert "[[resolver-soft-fallback-0]]" in body
        # The framer saw the concept id + its member summaries.
        assert seen["concept"] == "resolver-pattern"
        assert seen["members"], "framer received the member summaries"

    @pytest.mark.asyncio
    async def test_framer_failure_falls_back_to_deterministic_body(
        self, workspace_storage: FileSystemStorage
    ) -> None:
        """A framer that raises (routing miss surfaced as None is handled by the
        factory; here we cover an LLM error) must NOT break promotion — the
        concept still gets the deterministic Lift 1 MOC body."""
        for i in range(2):
            await workspace_storage.write(
                f"garden/seedling/resolver-soft-fallback-{i}.md",
                "---\ntags:\n  - settle\n  - resolver-pattern\n---\n"
                f"# Resolver soft fallback {i}\n\nResolvers degrade on a miss.\n",
            )

        class _BoomFramer:
            async def frame(self, *, concept: str, members: list[tuple[str, str]]) -> str | None:
                raise RuntimeError("framing model down")

        service = await _make_service(workspace_storage, safe_mode=False)
        await GardenObservationPromoter(service, framer=_BoomFramer()).promote()

        match = [
            p
            for p in await workspace_storage.list_files("concepts/active")
            if "resolver-pattern" in p
        ]
        assert match, "promotion survived the framer failure"
        body = await workspace_storage.read(match[0])
        # Deterministic Lift 1 body still present.
        assert "Synthesized from" in body
        assert "[[resolver-soft-fallback-0]]" in body
