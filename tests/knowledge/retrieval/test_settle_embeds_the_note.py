"""The settle hook must embed the NOTE, not the work-log line that produced it.

Two places answer "what text represents this note for retrieval?":

* `embed_and_store_note` (shared by the live subscriber AND the reconcile
  backfill) reads the note and embeds ``title + body``.
* `build_note_embed_hook`'s settle hook embedded ``settlement.summary`` — the
  one-line work-log sentence, never the note's own words.

The settle hook duplicated the logic instead of reusing it, and the copy picked
a different answer. Measured against prod (2026-08-26, ollama/bge-m3), for a
real stored vector:

    positive control (same text, re-embedded) : 1.0000
    stored vector vs the settle `summary`     : 1.0000   ← what it was keyed to
    stored vector vs the note's own body      : 0.7006

So semantic recall over settled knowledge was ranked by the work-log sentence.
And it could not self-heal: `reconcile_embeddings` skips any path already in
`existing_paths()` for the current model, so a note that HAS a (wrong) vector is
never re-embedded. The one path that writes the right text is the one blocked
from ever running on these notes.

⚠️ This hook's only tests lived in the Ollama-gated e2e files — exactly the 4
that CI skips. The defect lived where CI does not look, so these run with a
stub embedder and no Ollama.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


class _StubEmbedder:
    """Records every text handed to the embedder."""

    enabled = True
    model = "stub/model"

    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        return [0.1, 0.2, 0.3]


def _write_note(vault_root: Path, *, region: str, workspace_id: uuid.UUID, rel: str) -> Path:
    path = vault_root / region / str(workspace_id) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: Worker staleness needs a reference point\n---\n\n"
        "A process start time alone cannot say whether the code is old; it has to be\n"
        "compared against HEAD's commit time.\n",
        encoding="utf-8",
    )
    return path


async def _run_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _StubEmbedder:
    from backend.knowledge.infrastructure.workers.settle_worker import Settlement
    from backend.workflow.application.runtime import settle_runtime

    region, workspace_id = "us-1", uuid.uuid4()
    rel = "garden/seedling/staleness.md"
    note = _write_note(tmp_path, region=region, workspace_id=workspace_id, rel=rel)

    embedder = _StubEmbedder()
    stored: list[tuple[str, list[float]]] = []

    monkeypatch.setattr(
        "backend.knowledge.retrieval.embedder_resolution.resolve_knowledge_embedder",
        lambda _s: embedder,
    )

    class _Backend:
        def __init__(self, *_a: object, **_kw: object) -> None: ...

        async def store(self, note_path: str, vector: list[float]) -> None:
            stored.append((note_path, vector))

        async def existing_paths(self) -> set[str]:
            return set()

    monkeypatch.setattr("backend.knowledge.retrieval.storage.pg.PgNoteVectorBackend", _Backend)

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_a: object) -> None: ...

        async def commit(self) -> None: ...

    settings = settle_runtime.get_settings()
    monkeypatch.setattr(settings, "knowledge_vault_root", str(tmp_path), raising=False)

    hook = settle_runtime.build_note_embed_hook(
        session_factory=lambda: _Session(),  # type: ignore[arg-type]
        settings=settings,
    )
    await hook(
        Settlement(
            workspace_id=workspace_id,
            region=region,
            run_id=uuid.uuid4(),
            activity_id=uuid.uuid4(),
            verified=True,
            summary="Delivered the staleness CLI — 3 files changed.",
        ),
        str(note),
    )
    return embedder


async def test_the_settle_hook_embeds_the_notes_own_words(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The note's body is the knowledge. That is what a semantic search must match."""
    embedder = await _run_hook(tmp_path, monkeypatch)
    assert embedder.embedded, "the hook embedded nothing"
    text = embedder.embedded[0]
    assert "compared against HEAD's commit time" in text


async def test_the_settle_hook_does_not_embed_the_work_log_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Delivered the staleness CLI — 3 files changed" is provenance, not knowledge.
    Keying the vector to it is what made recall rank by the wrong sentence."""
    embedder = await _run_hook(tmp_path, monkeypatch)
    text = embedder.embedded[0]
    assert "3 files changed" not in text


async def test_the_settle_hook_includes_the_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`embed_and_store_note` embeds ``title + body``; the settle path must key
    its vectors the SAME way or the two writers drift apart again."""
    embedder = await _run_hook(tmp_path, monkeypatch)
    assert "Worker staleness needs a reference point" in embedder.embedded[0]
