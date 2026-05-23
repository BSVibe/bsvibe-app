"""/api/v1/decisions/{proposal_id}/{accept,reject} — founder resolution.

These endpoints resolve a queued canonicalization proposal against the
per-workspace **vault** (FS-as-SoT) via
:meth:`CanonicalizationService.accept_proposal` / ``reject_proposal``.

The proposal id is its vault path (the engine's natural handle — the
:class:`CanonicalizationService` addresses proposals by ``proposal_path``).
Real, queued proposals are produced by the Safe-Mode promotion pipeline, so
the fixtures drive ``GardenObservationPromoter`` exactly like
``tests/knowledge/canonicalization/test_promotion_e2e.py`` to materialise a
genuine ``pending`` merge proposal in the workspace vault, then exercise the
HTTP surface end-to-end (auth + workspace scope + service construction).
"""

from __future__ import annotations

import urllib.parse
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from backend.api.deps import get_current_user, get_workspace_id
from backend.api.main import create_app
from backend.api.v1.decisions import build_canonicalization_service
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.canonicalization.lock import AsyncIOMutationLock
from backend.knowledge.canonicalization.promotion import GardenObservationPromoter
from backend.knowledge.canonicalization.resolver import TagResolver
from backend.knowledge.canonicalization.service import CanonicalizationService
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.markdown_utils import extract_frontmatter
from backend.knowledge.graph.storage import FileSystemStorage

from .._support import fake_current_user

pytestmark = pytest.mark.asyncio


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
    spellings (``self-hosting`` / ``self-host``) plus an unrelated entity."""
    for i in range(4):
        await storage.write(
            f"garden/seedling/settle-self-hosting-{i}.md",
            "---\ntags:\n  - settle\n  - verified-run\n  - self-hosting\n---\n# obs\n",
        )
    await storage.write(
        "garden/seedling/settle-self-host.md",
        "---\ntags:\n  - settle\n  - verified-run\n  - self-host\n---\n# obs\n",
    )
    await storage.write(
        "garden/seedling/settle-vaultwarden.md",
        "---\ntags:\n  - settle\n  - verified-run\n  - vaultwarden\n---\n# obs\n",
    )


async def _seed_queued_merge_proposal(storage: FileSystemStorage) -> str:
    """Create a real ``pending`` merge proposal in the workspace vault.

    Mirrors test_promotion_e2e's Safe-Mode-with-existing-concepts path: make
    the two variant concepts active under a permissive service, then promote
    under Safe Mode so the clustered merge is QUEUED (proposal stays pending,
    its merge action sits at ``pending_approval``). Returns the proposal path.
    """
    permissive = await _make_service(storage, safe_mode=False)
    for cid in ("self-hosting", "self-host"):
        draft = await permissive.create_action_draft(
            kind="create-concept", params={"concept": cid, "title": cid}
        )
        await permissive.apply_action(draft, actor="seed")
    await _seed_garden_observations(storage)

    safe = await _make_service(storage, safe_mode=True)
    result = await GardenObservationPromoter(safe).promote()
    assert len(result.proposals) == 1, result.proposals
    proposal_path = result.proposals[0]
    fm = extract_frontmatter(await storage.read(proposal_path))
    assert fm["status"] == "pending"
    return proposal_path


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_storage(vault_root: Path, workspace_id: uuid.UUID) -> FileSystemStorage:
    """Storage rooted exactly like the request handler's per-workspace vault:
    ``<vault_root>/<region>/<workspace_id>/``."""
    root = vault_root / "us-1" / str(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(root)


@pytest_asyncio.fixture
async def client(vault_root: Path, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _service(ws: uuid.UUID = workspace_id) -> CanonicalizationService:
        # Same construction the production dep uses, but rooted at the test
        # vault root so the request hits the seeded workspace vault. Safe Mode
        # is off for the resolution service (matching the production dep): a
        # founder accept applies the already-queued action via accept_proposal
        # → apply_action, which would re-queue under Safe Mode.
        storage = FileSystemStorage(vault_root / "us-1" / str(ws))
        return await _make_service(storage, safe_mode=False)

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[build_canonicalization_service] = _service

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _enc(proposal_path: str) -> str:
    return urllib.parse.quote(proposal_path, safe="")


async def test_accept_applies_merge_and_collapses_variant(client, workspace_storage) -> None:
    proposal_path = await _seed_queued_merge_proposal(workspace_storage)

    r = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/accept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal_path"] == proposal_path
    assert body["status"] == "accepted"
    # The linked merge action applied → one affected-path-bearing result.
    assert any(rr["final_status"] == "applied" for rr in body["results"])

    # Proposal note flipped to accepted in the vault.
    fm = extract_frontmatter(await workspace_storage.read(proposal_path))
    assert fm["status"] == "accepted"

    # The merge collapsed the variant pair: exactly one survives as canonical,
    # the other became a tombstone.
    survivors = {
        p.removeprefix("concepts/active/").removesuffix(".md")
        for p in await workspace_storage.list_files("concepts/active")
    }
    self_host_survivors = survivors & {"self-hosting", "self-host"}
    assert len(self_host_survivors) == 1
    merged = ({"self-hosting", "self-host"} - self_host_survivors).pop()
    assert await workspace_storage.exists(f"concepts/merged/{merged}.md")


async def test_reject_resolves_without_applying(client, workspace_storage) -> None:
    proposal_path = await _seed_queued_merge_proposal(workspace_storage)

    r = await client.post(
        f"/api/v1/decisions/{_enc(proposal_path)}/reject",
        json={"reason": "different concepts"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal_path"] == proposal_path
    assert body["status"] == "rejected"

    # Proposal flipped to rejected; nothing merged — both variants survive.
    fm = extract_frontmatter(await workspace_storage.read(proposal_path))
    assert fm["status"] == "rejected"
    assert await workspace_storage.exists("concepts/active/self-hosting.md")
    assert await workspace_storage.exists("concepts/active/self-host.md")
    assert await workspace_storage.list_files("concepts/merged") == []


async def test_reject_without_body_defaults_reason(client, workspace_storage) -> None:
    proposal_path = await _seed_queued_merge_proposal(workspace_storage)
    r = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/reject")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


async def test_accept_missing_proposal_404(client, workspace_storage) -> None:
    missing = "proposals/merge-concepts/20260523-000000-nope.md"
    r = await client.post(f"/api/v1/decisions/{_enc(missing)}/accept")
    assert r.status_code == 404, r.text


async def test_reject_missing_proposal_404(client, workspace_storage) -> None:
    missing = "proposals/merge-concepts/20260523-000000-nope.md"
    r = await client.post(f"/api/v1/decisions/{_enc(missing)}/reject")
    assert r.status_code == 404, r.text


async def test_cross_workspace_proposal_404(client, vault_root) -> None:
    """A proposal that lives in a DIFFERENT workspace's vault is invisible to
    the caller (per-workspace vault boundary) → 404, never resolved."""
    other_ws = uuid.uuid4()
    other_root = vault_root / "us-1" / str(other_ws)
    other_root.mkdir(parents=True, exist_ok=True)
    other_storage = FileSystemStorage(other_root)
    proposal_path = await _seed_queued_merge_proposal(other_storage)

    r = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/accept")
    assert r.status_code == 404, r.text
    # Untouched in the other workspace.
    fm = extract_frontmatter(await other_storage.read(proposal_path))
    assert fm["status"] == "pending"


async def test_double_accept_conflicts(client, workspace_storage) -> None:
    proposal_path = await _seed_queued_merge_proposal(workspace_storage)
    first = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/accept")
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/accept")
    assert second.status_code == 409, second.text


async def test_reject_after_reject_conflicts(client, workspace_storage) -> None:
    proposal_path = await _seed_queued_merge_proposal(workspace_storage)
    first = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/reject")
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/v1/decisions/{_enc(proposal_path)}/reject")
    assert second.status_code == 409, second.text


async def test_non_canon_path_rejected_404(client, workspace_storage) -> None:
    """A path that isn't a canon proposal path is not addressable → 404."""
    r = await client.post(f"/api/v1/decisions/{_enc('garden/seedling/x.md')}/accept")
    assert r.status_code == 404, r.text
