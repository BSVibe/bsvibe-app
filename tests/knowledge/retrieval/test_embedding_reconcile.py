"""Embedding backfill / reconcile (Lift 3).

Fills ``note_embeddings`` for vault knowledge notes that were never embedded —
the historical backlog (bulk-imported notes that bypassed the event path) and
concepts (which fire no write event). Idempotent: notes already embedded under
the current model are skipped; only the knowledge layers (garden + concepts)
are embedded, never the machinery (actions/proposals/decisions).
"""

from __future__ import annotations

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.retrieval.reconcile import reconcile_embeddings
from backend.knowledge.retrieval.storage.memory import InMemoryNoteVectorBackend

pytestmark = pytest.mark.asyncio


class _FakeEmbedder:
    """Embedder Protocol stand-in — records every text it embeds."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.calls: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def model(self) -> str | None:
        return "fake-model"

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [float(len(text) % 7), 1.0, 0.0]


def _write(vault: Vault, rel: str, title: str, body: str) -> None:
    p = vault.resolve_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: {title}\n---\n\n# {title}\n\n{body}\n")


async def test_reconcile_embeds_only_missing_notes(tmp_path) -> None:
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "Alpha principle body.")
    _write(vault, "garden/seedling/b.md", "B", "Beta principle body.")
    _write(vault, "concepts/active/c.md", "C", "Gamma synthesis.")
    store = InMemoryNoteVectorBackend()
    await store.store("garden/seedling/a.md", [1.0, 0.0, 0.0])  # already embedded
    embedder = _FakeEmbedder()

    result = await reconcile_embeddings(vault, embedder, store)

    assert result.scanned == 3
    assert result.embedded == 2  # b + c
    assert result.already == 1  # a skipped
    assert (await store.existing_paths()) == {
        "garden/seedling/a.md",
        "garden/seedling/b.md",
        "concepts/active/c.md",
    }
    # a.md (already embedded) was NOT re-embedded.
    assert "Alpha principle body." not in " ".join(embedder.calls)


async def test_reconcile_skips_machinery_layers(tmp_path) -> None:
    """actions/ (the create-concept log) etc. are machinery, not knowledge — never embedded."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "real knowledge")
    _write(vault, "actions/create-concept/x.md", "X", "machinery log entry")
    _write(vault, "proposals/merge-concepts/y.md", "Y", "a proposal")
    store = InMemoryNoteVectorBackend()

    result = await reconcile_embeddings(vault, _FakeEmbedder(), store)

    assert result.embedded == 1
    assert (await store.existing_paths()) == {"garden/seedling/a.md"}


async def test_reconcile_noop_when_embedder_disabled(tmp_path) -> None:
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "body")
    store = InMemoryNoteVectorBackend()

    result = await reconcile_embeddings(vault, _FakeEmbedder(enabled=False), store)

    assert result.disabled is True
    assert result.embedded == 0
    assert (await store.existing_paths()) == set()


async def test_reconcile_is_idempotent(tmp_path) -> None:
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "body")
    store = InMemoryNoteVectorBackend()
    embedder = _FakeEmbedder()

    first = await reconcile_embeddings(vault, embedder, store)
    second = await reconcile_embeddings(vault, embedder, store)

    assert first.embedded == 1
    assert second.embedded == 0
    assert second.already == 1
    assert len(embedder.calls) == 1  # the second pass embedded nothing
