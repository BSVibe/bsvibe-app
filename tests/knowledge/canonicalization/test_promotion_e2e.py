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
from backend.knowledge.graph.markdown_utils import extract_frontmatter
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
    unrelated entity, alongside the structural settle tags."""
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
    # ... and one with the variant spelling.
    await storage.write(
        "garden/seedling/settle-self-host.md",
        "---\n"
        "tags:\n"
        "  - settle\n"
        "  - verified-run\n"
        "  - self-host\n"
        "---\n"
        "# Settle: hardened self host\n",
    )
    # An unrelated entity that must NOT cluster with the self-host* pair.
    await storage.write(
        "garden/seedling/settle-vaultwarden.md",
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
        assert result.applied_merges == []

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
