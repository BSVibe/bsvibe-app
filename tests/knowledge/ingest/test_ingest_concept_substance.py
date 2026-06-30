"""Ingest produces substantive concepts, not empty stubs (import-pipeline K1 + noise).

The ingest path auto-creates a concept for every recurring tag. Pre-fix it
called ``resolve_and_canonicalize`` with no ``initial_body``, so the concept was
born as an empty ``# Title`` shell (the K1 anti-pattern the KG redesign fixed
ONLY on the promoter path — ingest still produced empty concepts, surfaced by a
live fresh import: 21/57 concepts empty). And it created an empty entity stub
for every ``[[Name]]`` mention — the E20 auto-stub noise. Both are fixed:

* fix-2: the auto-created concept is born with a body distilled from the
  founding note's content (substance, not a shell).
* fix-3: empty ``[[Name]]`` stubs are no longer generated — a node exists only
  when it has substance; a dangling link becomes a node when the entity is
  actually canonicalized.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.knowledge.ingest.ingest_compiler._actions import execute_plan

pytestmark = pytest.mark.asyncio


class _FakeCanon:
    """Captures the (raw_tag, initial_body) passed to resolve_and_canonicalize."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def resolve_and_canonicalize(
        self,
        raw_tag: str,
        *,
        raw_source: str,
        note_type: str | None = None,
        initial_body: str | None = None,
    ) -> str:
        self.calls.append((raw_tag, initial_body))
        return raw_tag  # treat as an auto-created concept


class _FakeWriter:
    def __init__(self) -> None:
        self.stub_calls: list[str] = []

    async def write_garden(self, note: Any) -> Path:
        return Path(f"garden/seedling/{uuid.uuid4().hex}.md")

    async def update_note(self, path: str, content: str) -> Path:
        return Path(path)

    async def append_to_note(self, path: str, content: str) -> Path:
        return Path(path)

    async def ensure_entity_stub(self, name: str, mentioned_in: Path) -> None:
        self.stub_calls.append(name)


_CONTENT = (
    "Constant-time comparison guards every secret check so an attacker can't "
    "learn bytes from response timing. Use [[compare-digest]] at the auth boundary."
)


async def test_ingest_concept_born_with_body_from_founding_note() -> None:
    """fix-2 — the auto-created concept carries a body distilled from the note
    content that introduced it, not an empty title shell (K1 on the ingest path)."""
    canon = _FakeCanon()
    writer = _FakeWriter()
    plan = [
        {
            "action": "create",
            "title": "Timing attack guard",
            "content": _CONTENT,
            "reason": "recurring security pattern",
            "tags": ["timing-attack-prevention"],
            "entities": ["[[compare-digest]]"],
        }
    ]

    await execute_plan(writer, canon, plan, max_updates=10)

    assert canon.calls, "the tag was canonicalized"
    raw_tag, initial_body = canon.calls[0]
    assert raw_tag == "timing-attack-prevention"
    # The concept is born substantive — a real excerpt of the founding note.
    assert initial_body, "ingest-created concept must carry a body (not an empty shell)"
    assert "Constant-time comparison" in initial_body


async def test_ingest_does_not_generate_empty_entity_stubs() -> None:
    """fix-3 — a ``[[Name]]`` mention no longer spawns an empty stub node (the
    E20 auto-stub noise). The node appears only when the entity is canonicalized
    with substance; a dangling link is fine until then."""
    canon = _FakeCanon()
    writer = _FakeWriter()
    plan = [
        {
            "action": "create",
            "title": "Timing attack guard",
            "content": _CONTENT,
            "reason": "r",
            "tags": ["timing-attack-prevention"],
            "entities": ["[[compare-digest]]"],
        }
    ]

    await execute_plan(writer, canon, plan, max_updates=10)

    assert writer.stub_calls == [], "no empty entity stubs should be generated"
