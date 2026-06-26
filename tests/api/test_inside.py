"""/api/v1/inside — the founder's read-only window into the knowledge graph.

These tests exercise the HTTP surface end-to-end (router-level auth + workspace
scope + vault-scoped reader construction) against a real per-workspace vault on
disk:

* ``GET /inside/concepts`` lists the canonical anchors the canonicalization
  promoter graduated. Anchors are created by driving the *permissive*
  :class:`GardenObservationPromoter` path from
  ``tests/knowledge/canonicalization/test_promotion_e2e.py`` so the test seeds
  REAL ``concepts/active/<id>.md`` notes via the production engine, not
  hand-written fixtures.
* ``GET /inside/observations`` lists the raw garden observation notes the
  SettleWorker writes — seeded here through the SAME
  :class:`~backend.knowledge.graph.writer.GardenWriter`/:class:`GardenNote`
  path the sink uses, so the on-disk shape (``garden/seedling/...`` +
  ``captured_at`` + ``tags``) matches production.

Workspace isolation is structural: each request's reader is rooted at the
caller's ``<vault_root>/<region>/<workspace_id>/``, so another workspace's vault
is never enumerated. An empty workspace yields empty lists, never an error.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.api.deps import get_current_user, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.inside import build_inside_index, build_inside_storage
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.promotion import GardenObservationPromoter
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote, GardenWriter

from .._support import fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"


# ---------------------------------------------------------------------------
# Seed helpers — drive the REAL engine / writer, not hand-written fixtures.
# ---------------------------------------------------------------------------
async def _make_service(storage: FileSystemStorage, *, safe_mode: bool) -> CanonicalizationService:
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        safe_mode=lambda: safe_mode,
    )


async def _seed_garden_observations(storage: FileSystemStorage) -> None:
    """settle-style observations referencing one entity under two variant
    spellings plus an unrelated entity (mirrors test_promotion_e2e).

    Every entity recurs across >= 2 observations so it clears the promoter's
    recurrence gate (``_MIN_OBSERVATIONS_FOR_PROMOTION``)."""
    for i in range(4):
        await storage.write(
            f"garden/seedling/settle-self-hosting-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - self-hosting\n---\n# obs\n",
        )
    for i in range(2):
        await storage.write(
            f"garden/seedling/settle-self-host-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - self-host\n---\n# obs\n",
        )
    for i in range(2):
        await storage.write(
            f"garden/seedling/settle-vaultwarden-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - vaultwarden\n---\n# obs\n",
        )


async def _seed_anchors(storage: FileSystemStorage) -> set[str]:
    """Materialise REAL canonical anchors in the vault via the promoter.

    Permissive policy → the promoter auto-applies ``create-concept`` actions,
    leaving ``concepts/active/<id>.md`` notes (the "wall"). Returns the set of
    surviving canonical concept ids (the self-host* pair merges to one).
    """
    permissive = await _make_service(storage, safe_mode=False)
    await _seed_garden_observations(storage)
    await GardenObservationPromoter(permissive).promote()
    return {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await storage.list_files("concepts/active")
    }


async def _seed_settle_observation(vault_root: Path, workspace_id: uuid.UUID, title: str) -> None:
    """Write one garden observation via the production GardenWriter path.

    Same write surface :class:`KnowledgeSettleSink` uses (GardenNote →
    ``write_garden`` → ``garden/seedling/<slug>.md`` with ``captured_at`` +
    ``tags``), rooted at the per-workspace vault.
    """
    ws_root = vault_root / _REGION / str(workspace_id)
    ws_root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(Vault(ws_root))
    await writer.write_garden(
        GardenNote(
            title=title,
            content="A verified work step left this observation.",
            source="settle_worker",
            knowledge_layer="episodic",
            tags=["settle", "verified-run", "auth"],
        )
    )


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/api/test_decisions_resolve.py
# ---------------------------------------------------------------------------
@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_storage(vault_root: Path, workspace_id: uuid.UUID) -> FileSystemStorage:
    """Storage rooted exactly like the request handler's per-workspace vault."""
    root = vault_root / _REGION / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(root)


@pytest_asyncio.fixture
async def client(vault_root: Path, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _storage(ws: uuid.UUID = workspace_id) -> FileSystemStorage:
        root = vault_root / _REGION / str(ws)
        root.mkdir(parents=True, exist_ok=True)
        return FileSystemStorage(root)

    async def _index(ws: uuid.UUID = workspace_id) -> InMemoryCanonicalizationIndex:
        index = InMemoryCanonicalizationIndex()
        await index.initialize(FileSystemStorage(vault_root / _REGION / str(ws)))
        return index

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[build_inside_storage] = _storage
    app.dependency_overrides[build_inside_index] = _index

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# concepts
# ---------------------------------------------------------------------------
async def test_concepts_lists_canonical_anchors(client, workspace_storage) -> None:
    """GET /inside/concepts surfaces the real promoted anchors."""
    survivors = await _seed_anchors(workspace_storage)

    r = await client.get("/api/v1/inside/concepts")
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert ids == survivors
    # vaultwarden is untouched by the merge → always a surviving anchor.
    assert "vaultwarden" in ids
    row = next(row for row in rows if row["id"] == "vaultwarden")
    # name is the concept display title, and the response shape is stable.
    assert row["name"]
    assert isinstance(row["aliases"], list)
    assert row["alias_count"] == len(row["aliases"])
    assert row["created_at"]
    assert row["updated_at"]


async def test_concepts_response_surfaces_note_type(client, workspace_storage) -> None:
    """Lift E28 — ``GET /inside/concepts`` returns each concept's ``type``
    field (E26/E27 propagated) so the founder can skim by Pattern /
    Principle / TechInsight / DomainModel instead of the all-``concept``
    pre-E28 collapse."""
    from backend.knowledge.canonicalization import models as _models
    from backend.knowledge.canonicalization.store import NoteStore as _NoteStore

    # Bypass the promotion path and write a typed concept directly so the
    # test stays focused on the API surface, not the promotion plumbing.
    store = _NoteStore(workspace_storage)
    await store.write_concept(
        _models.ConceptEntry(
            concept_id="pipe-drain",
            path="concepts/active/pipe-drain.md",
            display="Pipe drain",
            aliases=[],
            created_at=datetime(2026, 6, 14, tzinfo=UTC),
            updated_at=datetime(2026, 6, 14, tzinfo=UTC),
            note_type="Pattern",
        )
    )

    r = await client.get("/api/v1/inside/concepts")
    assert r.status_code == 200, r.text
    rows = r.json()
    row = next(row for row in rows if row["id"] == "pipe-drain")
    assert row["type"] == "Pattern", "E28 — concept list response must surface the note kind"


async def test_concepts_empty_workspace(client) -> None:
    """No anchors → empty list, not an error."""
    r = await client.get("/api/v1/inside/concepts")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_concepts_limit_capped(client, workspace_storage) -> None:
    """A limit honours the cap; over-limit is rejected by validation."""
    await _seed_anchors(workspace_storage)
    r = await client.get("/api/v1/inside/concepts", params={"limit": 1})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1

    over = await client.get("/api/v1/inside/concepts", params={"limit": 9999})
    assert over.status_code == 422, over.text


async def test_concepts_workspace_isolation(client, vault_root) -> None:
    """Anchors in a DIFFERENT workspace's vault are never enumerated."""
    other_ws = uuid.uuid4()
    other_root = vault_root / _REGION / str(other_ws)
    other_root.mkdir(parents=True, exist_ok=True)
    await _seed_anchors(FileSystemStorage(other_root))

    r = await client.get("/api/v1/inside/concepts")
    assert r.status_code == 200, r.text
    assert r.json() == []


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------
async def test_observations_lists_garden_notes(client, vault_root, workspace_id) -> None:
    """GET /inside/observations surfaces the raw settle garden notes."""
    await _seed_settle_observation(vault_root, workspace_id, "Wired the auth callback")
    await _seed_settle_observation(vault_root, workspace_id, "Fixed the JWKS probe")

    r = await client.get("/api/v1/inside/observations")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 2
    titles = {row["title"] for row in rows}
    assert titles == {"Wired the auth callback", "Fixed the JWKS probe"}
    row = rows[0]
    assert row["id"].startswith("garden/")
    assert "settle" in row["tags"]
    assert row["excerpt"]
    assert row["captured_at"]


async def test_observations_empty_workspace(client) -> None:
    """No garden notes → empty list, not an error."""
    r = await client.get("/api/v1/inside/observations")
    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_observations_limit_capped(client, vault_root, workspace_id) -> None:
    """A limit caps the returned rows; over-limit is rejected by validation."""
    for i in range(3):
        await _seed_settle_observation(vault_root, workspace_id, f"Observation {i}")
    r = await client.get("/api/v1/inside/observations", params={"limit": 2})
    assert r.status_code == 200, r.text
    assert len(r.json()) == 2

    over = await client.get("/api/v1/inside/observations", params={"limit": 9999})
    assert over.status_code == 422, over.text


async def test_observations_workspace_isolation(client, vault_root) -> None:
    """Garden notes in a DIFFERENT workspace's vault are never enumerated."""
    other_ws = uuid.uuid4()
    await _seed_settle_observation(vault_root, other_ws, "Other workspace work")

    r = await client.get("/api/v1/inside/observations")
    assert r.status_code == 200, r.text
    assert r.json() == []


# ---------------------------------------------------------------------------
# note (R12) — the report's knowledge deep-link target
# ---------------------------------------------------------------------------
async def test_note_returns_a_written_note(
    client, workspace_storage, vault_root, workspace_id
) -> None:
    """GET /inside/note returns one note's title + body (frontmatter stripped),
    so the report's '추가한 지식' chip can show the actual note the run wrote."""
    await _seed_settle_observation(vault_root, workspace_id, "Add a mean utility")
    paths = await workspace_storage.list_files("garden", "*.md")
    assert paths, "seed wrote a garden note"
    path = paths[0]

    r = await client.get("/api/v1/inside/note", params={"path": path})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == path
    assert body["title"]
    assert "verified work step left this observation" in body["content"]
    # The YAML frontmatter is stripped from the rendered content.
    assert "tags:" not in body["content"]


async def test_note_404_for_missing(client) -> None:
    r = await client.get(
        "/api/v1/inside/note", params={"path": "garden/seedling/does-not-exist.md"}
    )
    assert r.status_code == 404


async def test_note_404_for_traversal_or_non_note_path(client) -> None:
    for bad in ("../secret.md", "/etc/passwd", ".bsage/internal.md", "garden/x.txt"):
        r = await client.get("/api/v1/inside/note", params={"path": bad})
        assert r.status_code == 404, bad


async def test_note_is_workspace_scoped(client, vault_root) -> None:
    """A note in ANOTHER workspace's vault is not addressable — 404."""
    other_ws = uuid.uuid4()
    await _seed_settle_observation(vault_root, other_ws, "Other workspace note")
    other_root = FileSystemStorage(vault_root / _REGION / str(other_ws))
    paths = await other_root.list_files("garden", "*.md")
    assert paths
    # Same relative path, but the caller's vault doesn't contain it → 404.
    r = await client.get("/api/v1/inside/note", params={"path": paths[0]})
    assert r.status_code == 404
