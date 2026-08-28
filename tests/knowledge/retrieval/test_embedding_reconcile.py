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
    # a.md was stored WITHOUT a fingerprint (the pre-#838 shape), so it is
    # re-embedded rather than trusted — that is the backfill, not a regression.
    assert result.embedded == 3
    assert result.already == 0
    assert set(await store.existing_fingerprints()) == {
        "garden/seedling/a.md",
        "garden/seedling/b.md",
        "concepts/active/c.md",
    }
    # …and its vector is now keyed to the note's own words.
    assert "Alpha principle body." in " ".join(embedder.calls)


async def test_reconcile_skips_machinery_layers(tmp_path) -> None:
    """actions/ (the create-concept log) etc. are machinery, not knowledge — never embedded."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "real knowledge")
    _write(vault, "actions/create-concept/x.md", "X", "machinery log entry")
    _write(vault, "proposals/merge-concepts/y.md", "Y", "a proposal")
    store = InMemoryNoteVectorBackend()

    result = await reconcile_embeddings(vault, _FakeEmbedder(), store)

    assert result.embedded == 1
    assert set(await store.existing_fingerprints()) == {"garden/seedling/a.md"}


async def test_reconcile_noop_when_embedder_disabled(tmp_path) -> None:
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "body")
    store = InMemoryNoteVectorBackend()

    result = await reconcile_embeddings(vault, _FakeEmbedder(enabled=False), store)

    assert result.disabled is True
    assert result.embedded == 0
    assert set(await store.existing_fingerprints()) == set()


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


# ---------------------------------------------------------------------------
# A vector keyed to the WRONG text is worse than a missing one, and reconcile
# was structurally unable to see it
# ---------------------------------------------------------------------------
#
# `reconcile` skipped any path already present under the current model. That is
# right for "already embedded, unchanged" and wrong for everything else: a note
# whose vector was built from different text — a drifted writer, an edited note —
# kept its stale vector forever, because the one path that writes the correct
# text is the one that never looks at it.
#
# Measured in prod (2026-08-26, ollama/bge-m3) before #837: stored vectors sat at
# cosine 1.0000 against the settle work-log line and 0.7006 against the note's own
# body. #837 fixed the writer; those 1,724 rows stayed wrong.
#
# So the store now remembers WHAT it embedded. A row whose fingerprint is missing
# (every row written before this change) or no longer matches the note is
# re-embedded — the backfill IS this rule, not a one-shot script, so the same
# drift can never accumulate silently again.


async def test_a_row_with_no_fingerprint_is_re_embedded(tmp_path) -> None:
    """The backfill. Every pre-existing row has no fingerprint: unknown text is
    not evidence of correct text."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "Alpha principle body.")
    store = InMemoryNoteVectorBackend()
    await store.store("garden/seedling/a.md", [1.0, 0.0, 0.0])  # legacy row, no fingerprint
    embedder = _FakeEmbedder()

    result = await reconcile_embeddings(vault, embedder, store)

    assert result.embedded == 1
    assert "Alpha principle body." in " ".join(embedder.calls)


async def test_a_row_whose_fingerprint_still_matches_is_skipped(tmp_path) -> None:
    """POSITIVE CONTROL — reconcile must stay cheap. Re-embedding everything on
    every pass would turn a gap-filler into a full rebuild."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "Alpha principle body.")
    store = InMemoryNoteVectorBackend()
    embedder = _FakeEmbedder()
    await reconcile_embeddings(vault, embedder, store)  # first pass writes the fingerprint
    embedder.calls.clear()

    result = await reconcile_embeddings(vault, embedder, store)

    assert result.embedded == 0
    assert result.already == 1
    assert embedder.calls == []


async def test_an_edited_note_is_re_embedded(tmp_path) -> None:
    """The general case the fingerprint buys: the note changed, so its vector is
    stale. Previously this could only be fixed by deleting the row by hand."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "Alpha principle body.")
    store = InMemoryNoteVectorBackend()
    embedder = _FakeEmbedder()
    await reconcile_embeddings(vault, embedder, store)
    embedder.calls.clear()

    _write(vault, "garden/seedling/a.md", "A", "Alpha principle body, now corrected.")
    result = await reconcile_embeddings(vault, embedder, store)

    assert result.embedded == 1
    assert "now corrected" in " ".join(embedder.calls)


async def test_the_backfill_converges(tmp_path) -> None:
    """Run twice over a legacy row: the first pass fixes it, the second is a
    no-op. A backfill that re-fires forever is a cost leak, not a fix."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "Alpha principle body.")
    store = InMemoryNoteVectorBackend()
    await store.store("garden/seedling/a.md", [1.0, 0.0, 0.0])  # legacy row
    embedder = _FakeEmbedder()

    first = await reconcile_embeddings(vault, embedder, store)
    second = await reconcile_embeddings(vault, embedder, store)

    assert (first.embedded, second.embedded) == (1, 0)
    assert second.already == 1


# ---------------------------------------------------------------------------
# Bounded pass + per-batch durability (the 524 fix)
# ---------------------------------------------------------------------------
# Measured on prod 2026-08-28: a 1,685-note backfill ran ~12 minutes and
# committed ONCE at the end. Cloudflare cut the client at 125s, so every
# HTTP-shaped trigger — REST, MCP-over-HTTP, the PWA button — reported failure
# for work that actually succeeded. A surface that lies about failure is worse
# than no surface: the founder retries forever or gives up on a healthy index.
#
# The pass is fingerprint-keyed and therefore already idempotent, so the fix is
# not a job table — it is a bounded pass that commits as it goes and reports how
# much is left. "Call until remaining == 0" IS the resume protocol.


class _Checkpoints:
    """Records each checkpoint the reconcile asks its caller to durably commit."""

    def __init__(self) -> None:
        self.count = 0

    async def __call__(self) -> None:
        self.count += 1


async def test_bounded_pass_stops_at_max_embeds_and_reports_the_rest(tmp_path) -> None:
    vault = Vault(tmp_path)
    for i in range(5):
        _write(vault, f"garden/seedling/n{i}.md", f"N{i}", f"Body {i}.")
    store = InMemoryNoteVectorBackend()
    embedder = _FakeEmbedder()

    result = await reconcile_embeddings(vault, embedder, store, max_embeds=2)

    assert result.embedded == 2, "did not stop at the cap"
    assert result.remaining == 3, "did not report the un-embedded rest"
    assert len(embedder.calls) == 2, "embedded past the cap"


async def test_repeated_passes_drain_to_zero(tmp_path) -> None:
    """The resume protocol: call until ``remaining`` is 0. No job state needed."""
    vault = Vault(tmp_path)
    for i in range(5):
        _write(vault, f"garden/seedling/n{i}.md", f"N{i}", f"Body {i}.")
    store = InMemoryNoteVectorBackend()
    embedder = _FakeEmbedder()

    embedded_total = 0
    for _ in range(10):
        r = await reconcile_embeddings(vault, embedder, store, max_embeds=2)
        embedded_total += r.embedded
        if r.remaining == 0:
            break

    assert embedded_total == 5
    assert r.remaining == 0

    settled = await reconcile_embeddings(vault, embedder, store, max_embeds=2)
    assert settled.embedded == 0, "a settled corpus must not re-embed"
    assert settled.already == 5
    assert settled.remaining == 0


async def test_checkpoint_is_called_per_batch_so_a_cut_connection_keeps_the_work(
    tmp_path,
) -> None:
    """Without this the whole pass is one transaction — a cut loses 12 minutes."""
    vault = Vault(tmp_path)
    for i in range(5):
        _write(vault, f"garden/seedling/n{i}.md", f"N{i}", f"Body {i}.")
    store = InMemoryNoteVectorBackend()
    checkpoints = _Checkpoints()

    await reconcile_embeddings(
        vault, _FakeEmbedder(), store, checkpoint=checkpoints, checkpoint_every=2
    )

    assert checkpoints.count >= 2, "never checkpointed mid-pass"


async def test_settled_corpus_reports_zero_remaining(tmp_path) -> None:
    """Positive control: a clean index must not claim work is pending."""
    vault = Vault(tmp_path)
    _write(vault, "garden/seedling/a.md", "A", "Alpha.")
    store = InMemoryNoteVectorBackend()
    embedder = _FakeEmbedder()

    await reconcile_embeddings(vault, embedder, store)
    second = await reconcile_embeddings(vault, embedder, store)

    assert second.embedded == 0
    assert second.already == 1
    assert second.remaining == 0
