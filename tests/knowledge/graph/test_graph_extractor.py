"""Tests for GraphExtractor — rule-based entity/relationship extraction."""

from backend.knowledge.graph.graph_extractor import GraphExtractor
from backend.knowledge.graph.graph_models import ConfidenceLevel


def _make_note(
    *,
    note_type: str = "idea",
    title: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
    related: list[str] | None = None,
    body: str = "",
) -> str:
    """Build a minimal markdown note with frontmatter."""
    fm_lines = ["---"]
    fm_lines.append(f"type: {note_type}")
    if title:
        fm_lines.append(f"title: {title}")
    if tags:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    if source:
        fm_lines.append(f"source: {source}")
    if related:
        items = ", ".join(f"'[[{r}]]'" for r in related)
        fm_lines.append(f"related: [{items}]")
    fm_lines.append("---")
    fm_lines.append(body)
    return "\n".join(fm_lines)


def test_extract_note_entity():
    content = _make_note(title="BSage Project")
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("garden/idea/bsage.md", content)

    # v2.2: frontmatter type is used directly as entity_type (no mapping)
    note_entities = [e for e in entities if e.entity_type == "idea"]
    assert len(note_entities) == 1
    assert note_entities[0].name == "BSage Project"
    assert note_entities[0].source_path == "garden/idea/bsage.md"
    assert note_entities[0].confidence == ConfidenceLevel.EXTRACTED


def test_extract_note_entity_from_filename():
    """When no title in frontmatter, derive from filename."""
    content = _make_note()
    extractor = GraphExtractor()
    entities, _ = extractor.extract_from_note("garden/idea/my-cool-idea.md", content)

    # v2.2: entity_type = frontmatter type directly
    note_entities = [e for e in entities if e.entity_type == "idea"]
    assert note_entities[0].name == "my cool idea"


def test_extract_project_type():
    content = _make_note(note_type="project", title="BSage")
    extractor = GraphExtractor()
    entities, _ = extractor.extract_from_note("garden/idea/bsage.md", content)
    assert entities[0].entity_type == "project"


def test_extract_event_type():
    content = _make_note(note_type="event", title="Team Standup")
    extractor = GraphExtractor()
    entities, _ = extractor.extract_from_note("garden/event/standup.md", content)
    assert entities[0].entity_type == "event"


def test_extract_task_type():
    content = _make_note(note_type="task", title="Fix auth bug")
    extractor = GraphExtractor()
    entities, _ = extractor.extract_from_note("garden/task/fix-auth.md", content)
    assert entities[0].entity_type == "task"


def test_extract_tags():
    content = _make_note(tags=["ai", "productivity"])
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("a.md", content)

    tag_entities = [e for e in entities if e.entity_type == "tag"]
    assert len(tag_entities) == 2
    tag_names = {e.name for e in tag_entities}
    assert tag_names == {"ai", "productivity"}

    tagged_rels = [r for r in relationships if r.rel_type == "tagged_with"]
    assert len(tagged_rels) == 2


def test_extract_source():
    content = _make_note(source="telegram-input")
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("a.md", content)

    source_entities = [e for e in entities if e.entity_type == "source"]
    assert len(source_entities) == 1
    assert source_entities[0].name == "telegram-input"

    created_rels = [r for r in relationships if r.rel_type == "created_by"]
    assert len(created_rels) == 1


def test_extract_related_wikilinks():
    content = _make_note(related=["BSage", "Graph RAG"])
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("a.md", content)

    # v2.2: related targets get entity_type="concept" (not "note")
    related_entities = [
        e for e in entities if e.entity_type == "concept" and e.name in ("BSage", "Graph RAG")
    ]
    assert len(related_entities) == 2

    related_rels = [r for r in relationships if r.rel_type == "related_to"]
    assert len(related_rels) == 2


def test_extract_body_wikilinks():
    body = "This connects to [[Python]] and [[FastAPI]] for the backend."
    content = _make_note(body=body)
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("a.md", content)

    ref_rels = [r for r in relationships if r.rel_type == "references"]
    assert len(ref_rels) == 2
    ref_names = {e.name for e in entities if any(r.target_id == e.id for r in ref_rels)}
    assert ref_names == {"Python", "FastAPI"}


def test_body_wikilinks_dedup_with_related():
    """Body wikilinks that already appear in related are not duplicated."""
    body = "Also see [[BSage]] for more details."
    content = _make_note(related=["BSage"], body=body)
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("a.md", content)

    bsage_rels = [
        r
        for r in relationships
        if any(e.name == "BSage" and e.id in (r.source_id, r.target_id) for e in entities)
    ]
    # Should have related_to but NOT references (deduped)
    rel_types = {r.rel_type for r in bsage_rels}
    assert "related_to" in rel_types
    assert "references" not in rel_types


def test_body_wikilinks_dedup_within_body():
    """Same wikilink appearing multiple times in body produces only one reference."""
    body = "See [[Python]] and then [[Python]] again."
    content = _make_note(body=body)
    extractor = GraphExtractor()
    _, relationships = extractor.extract_from_note("a.md", content)

    ref_rels = [r for r in relationships if r.rel_type == "references"]
    assert len(ref_rels) == 1


def test_extract_no_frontmatter():
    """Gracefully handles notes without frontmatter."""
    content = "# Just a heading\n\nSome text with [[a link]]."
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("notes/plain.md", content)

    assert len(entities) >= 1  # note entity + link entity
    note_entities = [e for e in entities if e.source_path == "notes/plain.md"]
    assert len(note_entities) >= 1


def test_extract_empty_note():
    content = "---\ntype: idea\n---\n"
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("a.md", content)

    # Just the note entity, no tags/source/related
    assert len(entities) == 1
    assert len(relationships) == 0


def test_extract_properties_from_frontmatter():
    content = "---\ntype: idea\nstatus: growing\ncaptured_at: 2026-03-12\n---\n"
    extractor = GraphExtractor()
    entities, _ = extractor.extract_from_note("a.md", content)

    props = entities[0].properties
    assert props["type"] == "idea"
    assert props["status"] == "growing"
    assert str(props["captured_at"]) == "2026-03-12"


def test_extract_edge_types():
    """v2.2: frontmatter relations are strong, body wikilinks are weak."""
    body = "Also uses [[Docker]]."
    content = _make_note(related=["BSage"], tags=["ai"], body=body)
    extractor = GraphExtractor()
    _, relationships = extractor.extract_from_note("a.md", content)

    tagged = [r for r in relationships if r.rel_type == "tagged_with"]
    assert all(r.edge_type == "strong" for r in tagged)

    related = [r for r in relationships if r.rel_type == "related_to"]
    assert all(r.edge_type == "strong" for r in related)

    refs = [r for r in relationships if r.rel_type == "references"]
    assert all(r.edge_type == "weak" for r in refs)
    assert all(r.weight == 0.1 for r in refs)


def test_extract_typed_relations_with_ontology():
    """v2.2: frontmatter keys matching ontology relations create typed edges."""
    import asyncio

    from backend.knowledge.retrieval.ontology import OntologyRegistry

    async def _run():
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path

            registry = OntologyRegistry(Path(tmp) / "ontology.yaml")
            await registry.load()
            return registry

    registry = asyncio.run(_run())

    content = (
        "---\n"
        "type: event\n"
        "title: Team Standup\n"
        'attendees:\n  - "[[Alice]]"\n  - "[[Bob]]"\n'
        'belongs_to:\n  - "[[Project X]]"\n'
        "---\n"
        "Meeting notes here.\n"
    )
    extractor = GraphExtractor(ontology=registry)
    entities, relationships = extractor.extract_from_note("events/standup.md", content)

    attendee_rels = [r for r in relationships if r.rel_type == "attendees"]
    assert len(attendee_rels) == 2
    assert all(r.edge_type == "strong" for r in attendee_rels)
    assert all(r.weight == 1.0 for r in attendee_rels)

    belongs_rels = [r for r in relationships if r.rel_type == "belongs_to"]
    assert len(belongs_rels) == 1


def test_extract_knowledge_layer_from_frontmatter():
    """Post dynamic-ontology refactor knowledge_layer comes from frontmatter
    (or defaults to ``semantic``). The static type→layer mapping went away
    with the entity_types enum — callers that care about episodic /
    procedural now stamp the frontmatter explicitly."""
    content = "---\ntype: event\nknowledge_layer: episodic\ntitle: Standup\n---\n"
    extractor = GraphExtractor(ontology=None)
    entities, _ = extractor.extract_from_note("events/standup.md", content)

    assert entities[0].knowledge_layer == "episodic"


def test_extract_knowledge_layer_defaults_semantic():
    content = "---\ntype: idea\ntitle: A note\n---\n"
    extractor = GraphExtractor(ontology=None)
    entities, _ = extractor.extract_from_note("ideas/a-note.md", content)

    assert entities[0].knowledge_layer == "semantic"


def test_extract_fact_triple():
    """v2.2: Fact notes produce subject→object typed edge + supersedes chain."""
    content = (
        "---\n"
        "type: fact\n"
        "title: Blasin의 현재 역할\n"
        'subject: "[[Blasin]]"\n'
        "predicate: has_role\n"
        'object: "[[AX Team Lead]]"\n'
        "valid_from: 2024-06\n"
        "valid_to: present\n"
        "source_type: explicit\n"
        'supersedes: "[[fact-blasin-backend-dev]]"\n'
        "confidence: 0.99\n"
        "---\n"
        "AX 팀 리드.\n"
    )
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("facts/blasin-role.md", content)

    # Should have: fact note entity, Blasin, AX Team Lead, fact-blasin-backend-dev
    entity_names = {e.name for e in entities}
    assert "Blasin" in entity_names
    assert "AX Team Lead" in entity_names
    assert "fact-blasin-backend-dev" in entity_names

    # has_role edge: Blasin → AX Team Lead
    role_rels = [r for r in relationships if r.rel_type == "has_role"]
    assert len(role_rels) == 1
    assert role_rels[0].edge_type == "strong"

    # supersedes edge: this fact → old fact
    sup_rels = [r for r in relationships if r.rel_type == "supersedes"]
    assert len(sup_rels) == 1
    assert sup_rels[0].edge_type == "strong"


def test_extract_fact_without_supersedes():
    """Fact note without supersedes should still extract triple."""
    content = (
        "---\n"
        "type: fact\n"
        "title: Alice works at ACME\n"
        'subject: "[[Alice]]"\n'
        "predicate: works_at\n"
        'object: "[[ACME Corp]]"\n'
        "valid_from: 2024-01\n"
        "valid_to: present\n"
        "source_type: explicit\n"
        "---\n"
    )
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("facts/alice-acme.md", content)

    entity_names = {e.name for e in entities}
    assert "Alice" in entity_names
    assert "ACME Corp" in entity_names

    works_rels = [r for r in relationships if r.rel_type == "works_at"]
    assert len(works_rels) == 1

    sup_rels = [r for r in relationships if r.rel_type == "supersedes"]
    assert len(sup_rels) == 0


def test_extract_ambiguous_related():
    """v3.0: [[target]]? suffix marks relationships as AMBIGUOUS."""
    content = '---\ntype: person\ntitle: Bob\nrelated:\n  - "[[Alice]]"\n  - "[[Charlie]]?"\n---\n'
    extractor = GraphExtractor()
    entities, relationships = extractor.extract_from_note("people/bob.md", content)

    related_rels = [r for r in relationships if r.rel_type == "related_to"]
    assert len(related_rels) == 2

    alice_rel = [
        r for r in related_rels if any(e.name == "Alice" and e.id == r.target_id for e in entities)
    ]
    charlie_rel = [
        r
        for r in related_rels
        if any(e.name == "Charlie" and e.id == r.target_id for e in entities)
    ]
    assert alice_rel[0].confidence == ConfidenceLevel.EXTRACTED
    assert charlie_rel[0].confidence == ConfidenceLevel.AMBIGUOUS

    # Charlie entity itself should also be AMBIGUOUS
    charlie_ents = [e for e in entities if e.name == "Charlie"]
    assert charlie_ents[0].confidence == ConfidenceLevel.AMBIGUOUS
