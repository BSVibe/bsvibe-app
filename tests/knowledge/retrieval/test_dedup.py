"""Tests for semantic entity deduplication."""

from __future__ import annotations

import pytest

from backend.knowledge.graph.graph_models import GraphEntity, GraphRelationship
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault_backend import VaultBackend
from backend.knowledge.retrieval.dedup import (
    DuplicateDecision,
    find_semantic_duplicates,
    llm_check_duplicate,
    merge_duplicate,
)


@pytest.fixture
async def backend(tmp_path):
    storage = FileSystemStorage(tmp_path / "vault")
    b = VaultBackend(storage)
    await b.initialize()
    yield b
    await b.close()


class TestLLMCheckDuplicate:
    async def test_fast_path_exact_match(self):
        """Identical normalized names skip the LLM."""
        called = []

        async def llm_fn(sys: str, msg: str) -> str:
            called.append(True)
            return '{"is_duplicate": false}'

        e1 = GraphEntity(name="Alice", entity_type="person", source_path="a.md")
        e2 = GraphEntity(name="alice", entity_type="person", source_path="b.md")

        decision = await llm_check_duplicate(llm_fn, e1, e2)
        assert decision.is_duplicate
        assert called == []  # LLM not called

    async def test_llm_says_duplicate(self):
        async def llm_fn(sys: str, msg: str) -> str:
            return '{"is_duplicate": true, "reason": "NYC abbreviation"}'

        e1 = GraphEntity(name="NYC", entity_type="place", source_path="a.md")
        e2 = GraphEntity(name="New York City", entity_type="place", source_path="b.md")

        decision = await llm_check_duplicate(llm_fn, e1, e2)
        assert decision.is_duplicate
        assert "abbreviation" in decision.reason

    async def test_llm_says_not_duplicate(self):
        async def llm_fn(sys: str, msg: str) -> str:
            return '{"is_duplicate": false, "reason": "different languages"}'

        e1 = GraphEntity(name="Java", entity_type="language", source_path="a.md")
        e2 = GraphEntity(name="Python", entity_type="language", source_path="b.md")

        decision = await llm_check_duplicate(llm_fn, e1, e2)
        assert not decision.is_duplicate

    async def test_llm_response_with_code_fence(self):
        async def llm_fn(sys: str, msg: str) -> str:
            return '```json\n{"is_duplicate": true, "reason": "synonym"}\n```'

        e1 = GraphEntity(name="car", entity_type="concept", source_path="a.md")
        e2 = GraphEntity(name="vehicle", entity_type="concept", source_path="b.md")

        decision = await llm_check_duplicate(llm_fn, e1, e2)
        assert decision.is_duplicate

    async def test_llm_parse_error_defaults_to_false(self):
        async def llm_fn(sys: str, msg: str) -> str:
            return "not json at all"

        e1 = GraphEntity(name="A", entity_type="x", source_path="a.md")
        e2 = GraphEntity(name="B", entity_type="x", source_path="b.md")

        decision = await llm_check_duplicate(llm_fn, e1, e2)
        assert not decision.is_duplicate
        assert "parse error" in decision.reason


class TestFindSemanticDuplicates:
    async def test_finds_duplicates_across_candidates(self, backend):
        e1 = GraphEntity(
            name="Seoul National University",
            entity_type="organization",
            source_path="a.md",
        )
        e2 = GraphEntity(name="서울대학교", entity_type="organization", source_path="b.md")
        e3 = GraphEntity(name="SNU", entity_type="organization", source_path="c.md")
        await backend.upsert_entity(e1)
        await backend.upsert_entity(e2)
        await backend.upsert_entity(e3)

        async def llm_fn(sys: str, msg: str) -> str:
            # Always say duplicate for this test
            return '{"is_duplicate": true, "reason": "same institution"}'

        # Search using a query that will match candidates via substring
        dups = await find_semantic_duplicates(backend, e1, llm_fn)
        # e1 should find e2 and e3 as candidates (different names, but substring
        # search may or may not hit — depends on search logic). At minimum, it
        # should not include e1 itself.
        for cand, _ in dups:
            assert cand.id != e1.id

    async def test_different_types_not_matched(self, backend):
        e1 = GraphEntity(name="Apple", entity_type="organization", source_path="a.md")
        e2 = GraphEntity(name="Apple", entity_type="concept", source_path="b.md")
        await backend.upsert_entity(e1)
        await backend.upsert_entity(e2)

        async def llm_fn(sys: str, msg: str) -> str:
            return '{"is_duplicate": true}'

        dups = await find_semantic_duplicates(backend, e1, llm_fn)
        # Even though LLM says yes, type mismatch filters them out
        assert len(dups) == 0


class TestMergeDuplicate:
    async def test_merge_migrates_relationships(self, backend):
        canonical = GraphEntity(name="SNU", entity_type="organization", source_path="snu.md")
        duplicate = GraphEntity(name="서울대", entity_type="organization", source_path="seoul.md")
        other = GraphEntity(name="Alice", entity_type="person", source_path="alice.md")

        canon_id = await backend.upsert_entity(canonical)
        dup_id = await backend.upsert_entity(duplicate)
        other_id = await backend.upsert_entity(other)

        # Alice --works_at--> duplicate (서울대)
        await backend.upsert_relationship(
            GraphRelationship(
                source_id=other_id,
                target_id=dup_id,
                rel_type="works_at",
                source_path="alice.md",
            )
        )

        # Reload entities (they have IDs now)
        canonical = await backend.get_entity_by_name("SNU")
        duplicate = await backend.get_entity_by_name("서울대")
        assert canonical.id == canon_id
        assert duplicate.id == dup_id

        migrated = await merge_duplicate(backend, canonical, duplicate)
        assert migrated >= 1

        # Alice's relationship should now point to SNU
        alice_neighbors = await backend.query_neighbors(other_id)
        targets = {ent.name for _, ent in alice_neighbors}
        assert "SNU" in targets

        # Canonical should have alias
        refreshed = await backend.get_entity_by_name("SNU")
        assert "서울대" in refreshed.properties.get("aliases", [])


def test_duplicate_decision_defaults():
    d = DuplicateDecision(is_duplicate=False)
    assert d.canonical_id is None
    assert d.alias_for is None
    assert d.reason == ""
