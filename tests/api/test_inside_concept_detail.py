"""/api/v1/inside/concepts/{id} — the founder's concept inspector.

The Knowledge surface lists canonical anchors ("What I know") and draws the
force-directed graph, but a concept was not clickable into any *detail*. This
endpoint is the read-only inspector behind a clicked concept: it returns the
concept's display name + aliases, its **related concepts** (its neighbours in
:func:`backend.knowledge.canonicalization.concept_graph.build_concept_graph`,
with the co-occurrence weight), and its **source observations** — the garden
notes whose tags resolve onto this concept (title + short excerpt + date),
derived exactly the way the graph builder resolves tags → concepts.

Read-only. A 404 is returned when the id is not an active concept (a tombstone,
a deprecated id, or an unknown id is simply not on the wall).

These tests seed REAL active concepts (via the permissive
``CanonicalizationService``, exactly like ``test_inside_graph.py`` does) plus
garden observations whose tags reference those concepts, so the related-concept
edge weights and the source-observation list reflect the production build path —
no hand-written graph fixtures. Workspace isolation is structural: a concept
seeded in another workspace's vault is never inspectable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.api.deps import get_current_user, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.inside import build_inside_storage
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

from .._support import fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"
_FIXED_NOW = datetime(2026, 5, 24, 12, 0, 0)


# ---------------------------------------------------------------------------
# Seed helpers — create REAL active concepts + garden observations.
# ---------------------------------------------------------------------------
async def _make_permissive_service(storage: FileSystemStorage) -> CanonicalizationService:
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return CanonicalizationService(
        store=NoteStore(storage),
        lock=AsyncIOMutationLock(),
        index=index,
        resolver=TagResolver(index=index),
        clock=lambda: _FIXED_NOW,
        safe_mode=lambda: False,
    )


async def _seed_concepts(storage: FileSystemStorage, ids: list[str]) -> None:
    service = await _make_permissive_service(storage)
    for cid in ids:
        draft = await service.create_action_draft(
            kind="create-concept", params={"concept": cid, "title": cid}
        )
        await service.apply_action(draft, actor="test")


def _garden_note(title: str, body: str, *tags: str, captured_at: str | None = None) -> str:
    lines = ["---", "tags:"]
    lines += [f"  - {t}" for t in tags]
    if captured_at is not None:
        # Quoted exactly as the production GardenWriter serialises it (yaml.dump
        # quotes date-like strings) so it re-parses as a str, not a YAML date.
        lines.append(f"captured_at: '{captured_at}'")
    lines += ["---", f"# {title}", "", body, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/api/test_inside_graph.py
# ---------------------------------------------------------------------------
@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_storage(vault_root: Path, workspace_id: uuid.UUID) -> FileSystemStorage:
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

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[build_inside_storage] = _storage

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# detail — identity + aliases
# ---------------------------------------------------------------------------
async def test_detail_returns_identity_and_aliases(client, workspace_storage) -> None:
    """A concept's id, display name, and aliases come back on the wire."""
    await workspace_storage.write(
        "concepts/active/self-hosting.md",
        "---\n"
        "created_at: 2026-05-22T00:00:00\n"
        "updated_at: 2026-05-23T00:00:00\n"
        "aliases:\n"
        "  - self-host\n"
        "  - selfhosting\n"
        "---\n"
        "# Self-hosting\n",
    )

    r = await client.get("/api/v1/inside/concepts/self-hosting")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "self-hosting"
    assert body["name"] == "Self-hosting"
    assert set(body["aliases"]) == {"self-host", "selfhosting"}
    # Shape is stable — related + observations always present (possibly empty).
    assert isinstance(body["related"], list)
    assert isinstance(body["observations"], list)


# ---------------------------------------------------------------------------
# detail — related concepts (graph neighbours + weight)
# ---------------------------------------------------------------------------
async def test_detail_lists_related_neighbours_with_weight(client, workspace_storage) -> None:
    """Related = the concept's neighbours in build_concept_graph, with the
    co-occurrence weight. auth + jwks co-occur in two observations → weight 2."""
    await _seed_concepts(workspace_storage, ["auth", "jwks", "lonely"])
    for i in range(2):
        await workspace_storage.write(
            f"garden/seedling/obs-{i}.md",
            _garden_note("obs", "body", "settle", "verified-run", "auth", "jwks"),
        )

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    related = r.json()["related"]
    by_id = {rel["id"]: rel for rel in related}
    # jwks is the only neighbour; lonely never co-occurs with auth.
    assert set(by_id) == {"jwks"}
    assert by_id["jwks"]["name"] == "jwks"
    assert by_id["jwks"]["weight"] == 2.0


async def test_detail_related_empty_for_unconnected_concept(client, workspace_storage) -> None:
    """A concept with no graph neighbours returns related == []."""
    await _seed_concepts(workspace_storage, ["lonely"])

    r = await client.get("/api/v1/inside/concepts/lonely")
    assert r.status_code == 200, r.text
    assert r.json()["related"] == []


# ---------------------------------------------------------------------------
# detail — source observations (origin / usage)
# ---------------------------------------------------------------------------
async def test_detail_lists_source_observations(client, workspace_storage) -> None:
    """Origin/usage = the garden observations whose tags resolve to this
    concept — title + short excerpt + date — resolved the SAME way the graph
    builder resolves tags → concepts."""
    await _seed_concepts(workspace_storage, ["auth", "jwks"])
    await workspace_storage.write(
        "garden/seedling/obs-a.md",
        _garden_note(
            "Wired the auth callback",
            "Founder confirmed the redirect target.",
            "settle",
            "verified-run",
            "auth",
            captured_at="2026-05-20T00:00:00",
        ),
    )
    # An observation that references jwks only — must NOT appear under auth.
    await workspace_storage.write(
        "garden/seedling/obs-b.md",
        _garden_note("JWKS probe", "body", "settle", "jwks"),
    )

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    observations = r.json()["observations"]
    titles = {o["title"] for o in observations}
    assert titles == {"Wired the auth callback"}
    obs = observations[0]
    assert obs["id"] == "garden/seedling/obs-a.md"
    assert "redirect target" in obs["excerpt"]
    assert obs["captured_at"] == "2026-05-20T00:00:00"
    # The inspector renders the note's FULL body (not just the one-line excerpt)
    # so a clicked concept reads as a readable note.
    assert obs["body"] == "Founder confirmed the redirect target."
    assert obs["truncated"] is False


async def test_detail_observation_body_preserves_multiline(client, workspace_storage) -> None:
    """The source observation body keeps its line breaks so the inspector can
    render it as a readable note (white-space: pre-wrap), not a flattened blurb."""
    await _seed_concepts(workspace_storage, ["auth"])
    multiline = "First paragraph line.\n\nSecond paragraph after a blank line.\n- a bullet"
    await workspace_storage.write(
        "garden/seedling/obs-multi.md",
        _garden_note("Multi-line note", multiline, "settle", "verified-run", "auth"),
    )

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    obs = r.json()["observations"][0]
    assert obs["body"] == multiline
    assert obs["truncated"] is False


async def test_detail_observation_body_strips_leading_h1_after_blank(
    client, workspace_storage
) -> None:
    """Real settle notes have a blank line between the frontmatter and the H1.
    The body must still drop that leading H1 (it's already the note title) so the
    inspector doesn't show the heading twice."""
    await _seed_concepts(workspace_storage, ["auth"])
    # Built by hand to mirror the GardenWriter: a BLANK line precedes the "# " H1.
    note = (
        "---\n"
        "tags:\n"
        "  - settle\n"
        "  - verified-run\n"
        "  - auth\n"
        "captured_at: '2026-05-24'\n"
        "---\n"
        "\n"
        "# Settle: wired the auth callback\n"
        "\n"
        "Founder confirmed the redirect target.\n"
    )
    await workspace_storage.write("garden/seedling/obs-blank-h1.md", note)

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    obs = r.json()["observations"][0]
    assert obs["body"] == "Founder confirmed the redirect target."
    assert not obs["body"].startswith("#")


async def test_detail_observation_body_strips_settle_footer(client, workspace_storage) -> None:
    """The inspector shows the note's CONTENT, not the SettleWorker's machine
    footer (Product / Intent / ## Artifacts / Verified / Run). The footer is a
    trailing block, so the body is just the LLM narrative above it."""
    await _seed_concepts(workspace_storage, ["auth"])
    # Mirrors SettleWorker._observation_body: narrative, then the metadata footer.
    note = (
        "---\n"
        "tags:\n"
        "  - settle\n"
        "  - verified-run\n"
        "  - auth\n"
        "captured_at: '2026-05-24'\n"
        "---\n"
        "# Settle: wired the auth callback\n"
        "\n"
        "Wired the OAuth callback and confirmed the redirect target.\n"
        "\n"
        "It now lands on /app.\n"
        "\n"
        "Product: bsvibe-site\n"
        "Intent: wire the auth callback\n"
        "## Artifacts\n"
        "- `src/auth.ts`\n"
        "\n"
        "Verified: yes\n"
        "Run: 6187b89b-92b7-4e68-8cc0-8aabebc32371\n"
    )
    await workspace_storage.write("garden/seedling/obs-footer.md", note)

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    obs = r.json()["observations"][0]
    # The narrative (incl. its inner line breaks) survives; the footer is gone.
    assert obs["body"] == (
        "Wired the OAuth callback and confirmed the redirect target.\n\nIt now lands on /app."
    )
    for marker in ("Intent:", "## Artifacts", "Verified:", "Run:", "Product:"):
        assert marker not in obs["body"]


async def test_detail_observation_body_truncated_when_huge(client, workspace_storage) -> None:
    """A body past the ~8KB cap is truncated and flagged so the wire stays bounded."""
    await _seed_concepts(workspace_storage, ["auth"])
    huge = "x" * 20000
    await workspace_storage.write(
        "garden/seedling/obs-huge.md",
        _garden_note("Huge note", huge, "settle", "verified-run", "auth"),
    )

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    obs = r.json()["observations"][0]
    assert obs["truncated"] is True
    assert len(obs["body"]) <= 8192


async def test_detail_observations_empty_when_no_reference(client, workspace_storage) -> None:
    """A concept never referenced by a garden note → observations == []."""
    await _seed_concepts(workspace_storage, ["auth"])

    r = await client.get("/api/v1/inside/concepts/auth")
    assert r.status_code == 200, r.text
    assert r.json()["observations"] == []


# ---------------------------------------------------------------------------
# detail — 404s
# ---------------------------------------------------------------------------
async def test_detail_unknown_id_404(client, workspace_storage) -> None:
    """An id that is not an active concept → 404, never a 500 or empty 200."""
    await _seed_concepts(workspace_storage, ["auth"])

    r = await client.get("/api/v1/inside/concepts/does-not-exist")
    assert r.status_code == 404, r.text


async def test_detail_empty_workspace_404(client) -> None:
    """A fresh workspace has no concepts → any id is a 404."""
    r = await client.get("/api/v1/inside/concepts/anything")
    assert r.status_code == 404, r.text


async def test_detail_workspace_isolation(client, vault_root) -> None:
    """A concept in a DIFFERENT workspace's vault is never inspectable."""
    other_ws = uuid.uuid4()
    other_root = vault_root / _REGION / str(other_ws)
    other_root.mkdir(parents=True, exist_ok=True)
    await _seed_concepts(FileSystemStorage(other_root), ["secret"])

    r = await client.get("/api/v1/inside/concepts/secret")
    assert r.status_code == 404, r.text
