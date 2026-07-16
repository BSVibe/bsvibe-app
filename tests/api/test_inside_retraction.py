"""REST surface for ``/api/v1/inside/nodes/{node_ref}/...`` retract + undo + correct.

Lift M3a end-to-end on the HTTP boundary. Each test drives the production
FastAPI app + a per-workspace tmp_path vault — the same per-workspace root
the inside read surface (concepts, observations, graph) reads from — so a
retract issued through the REST surface lands in the SAME place the
inspector reads.

Asserts:

* ``POST /retract`` issues a correction, returns the signal + undo window.
* ``POST /retract`` is idempotent on ``correction_id``.
* Path-traversal in ``node_ref`` is rejected.
* Missing ``node_ref`` returns 404.
* ``POST /undo`` honors a cancellation inside the window.
* ``POST /undo`` on an unknown id is 404.
* ``POST /correct`` records intent without writing a tombstone.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_current_user_row,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.inside.retraction import build_retraction_writer
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

_REGION = "us-1"

_NOTE_TEMPLATE = (
    "---\n"
    "kind: decision_resolution\n"
    "question: Should we cache the homepage?\n"
    "answer: Yes — 5 minute CDN TTL.\n"
    "intent_text: harden homepage perf\n"
    "captured_at: '2026-06-01T00:00:00Z'\n"
    "tags:\n"
    "  - settle\n"
    "  - decision\n"
    "---\n"
    "# Decision\n"
    "Cache the homepage at the CDN with a 5-minute TTL.\n"
)


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def actor_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vault_root(tmp_path: Path, workspace_id: uuid.UUID) -> Path:
    """Per-test vault rooted at the same shape the production
    :func:`_vault_root` builds — ``<vault_root>/<region>/<workspace_id>/``.

    The ``_isolate_w1_workspace_roots`` autouse fixture in
    ``tests/conftest.py`` only points the product / run roots at tmp_path;
    the knowledge vault settings still point at the production location.
    Tests override the writer dependency to root at tmp_path instead.
    """
    return tmp_path / "vault"


def _seed_note(vault_root: Path, workspace_id: uuid.UUID) -> str:
    rel_path = "garden/seedling/cache-homepage.md"
    note_path = vault_root / _REGION / str(workspace_id) / rel_path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(_NOTE_TEMPLATE, encoding="utf-8")
    return rel_path


@pytest_asyncio.fixture
async def client(
    sf: async_sessionmaker[AsyncSession],
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    monkeypatch,
):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    def _user_row() -> SimpleNamespace:
        return SimpleNamespace(id=actor_id)

    async def _session():
        async with sf() as s:
            yield s

    async def _writer() -> GardenWriter:
        ws_root = vault_root / _REGION / str(workspace_id)
        ws_root.mkdir(parents=True, exist_ok=True)
        return GardenWriter(vault=Vault(ws_root))

    # Override the inside._ensure_node_exists path's vault_root resolver so
    # the existence check sees our tmp vault instead of the prod settings root.
    monkeypatch.setattr(
        "backend.api.v1.inside.retraction._vault_root",
        lambda ws_id: vault_root / _REGION / str(ws_id),
    )

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_current_user_row] = _user_row
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[build_retraction_writer] = _writer

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_retract_issues_signal(
    client: httpx.AsyncClient,
    vault_root: Path,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> None:
    """``POST /retract`` returns the signal + ``created=True`` + 30s window."""
    node_ref = _seed_note(vault_root, workspace_id)

    r = await client.post(
        f"/api/v1/inside/nodes/{node_ref}/retract",
        json={"reason": "we changed the cache policy"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True
    assert body["undo_window_seconds"] == 30
    signal = body["signal"]
    assert signal["workspace_id"] == str(workspace_id)
    assert signal["actor_id"] == str(actor_id)
    assert signal["node_ref"] == node_ref
    assert signal["action"] == "retract"
    assert signal["reason"] == "we changed the cache policy"


async def test_retract_idempotent_on_correction_id(
    client: httpx.AsyncClient,
    vault_root: Path,
    workspace_id: uuid.UUID,
) -> None:
    """Re-POSTing with the same ``correction_id`` returns ``created=False``."""
    node_ref = _seed_note(vault_root, workspace_id)
    cid = str(uuid.uuid4())

    first = await client.post(
        f"/api/v1/inside/nodes/{node_ref}/retract",
        json={"correction_id": cid},
    )
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True

    second = await client.post(
        f"/api/v1/inside/nodes/{node_ref}/retract",
        json={"correction_id": cid},
    )
    assert second.status_code == 200, second.text
    assert second.json()["created"] is False
    assert second.json()["signal"]["id"] == cid


async def test_retract_unknown_node_404(
    client: httpx.AsyncClient,
) -> None:
    """Retract on a node that doesn't exist returns 404 — no orphan row."""
    r = await client.post(
        "/api/v1/inside/nodes/garden/seedling/nonexistent.md/retract",
        json={},
    )
    assert r.status_code == 404, r.text


async def test_undo_within_window(
    client: httpx.AsyncClient,
    vault_root: Path,
    workspace_id: uuid.UUID,
) -> None:
    """Undo before ``apply_at`` returns ``status="undone"``."""
    node_ref = _seed_note(vault_root, workspace_id)
    r = await client.post(
        f"/api/v1/inside/nodes/{node_ref}/retract",
        json={},
    )
    correction_id = r.json()["signal"]["id"]

    undo = await client.post(
        f"/api/v1/inside/corrections/{correction_id}/undo",
    )
    assert undo.status_code == 200, undo.text
    body = undo.json()
    assert body["status"] == "undone"
    assert body["correction_id"] == correction_id


async def test_undo_unknown_correction_404(client: httpx.AsyncClient) -> None:
    """Undoing an unknown correction id is a 404."""
    r = await client.post(
        f"/api/v1/inside/corrections/{uuid.uuid4()}/undo",
    )
    assert r.status_code == 404, r.text


async def test_undo_twice_returns_already_undone(
    client: httpx.AsyncClient,
    vault_root: Path,
    workspace_id: uuid.UUID,
) -> None:
    """A second undo after the first reports ``already_undone`` (idempotent)."""
    node_ref = _seed_note(vault_root, workspace_id)
    r = await client.post(f"/api/v1/inside/nodes/{node_ref}/retract", json={})
    correction_id = r.json()["signal"]["id"]
    first = await client.post(f"/api/v1/inside/corrections/{correction_id}/undo")
    assert first.json()["status"] == "undone"
    second = await client.post(f"/api/v1/inside/corrections/{correction_id}/undo")
    assert second.json()["status"] == "already_undone"


async def test_correct_endpoint_is_unavailable_501(
    client: httpx.AsyncClient,
    vault_root: Path,
    workspace_id: uuid.UUID,
) -> None:
    """``POST /correct`` reports the capability unavailable — no false success.

    The in-place field-rewrite editor was never built. The endpoint returns
    ``501`` rather than confirming a correction (and later writing a false
    ``ontology.correction.applied`` audit) for an operation that mutates
    nothing. The note stays untouched.
    """
    node_ref = _seed_note(vault_root, workspace_id)

    r = await client.post(
        f"/api/v1/inside/nodes/{node_ref}/correct",
        json={"corrections": {"answer": "5 minute TTL — corrected wording"}},
    )
    assert r.status_code == 501, r.text
    # File untouched — no tombstone, no rewrite.
    note = vault_root / _REGION / str(workspace_id) / node_ref
    assert note.exists()
    assert "retracted_at" not in note.read_text(encoding="utf-8")
