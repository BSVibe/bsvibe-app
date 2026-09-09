"""GDPR L1 endpoints — /api/v1/workspace/export + /processing-record.

Covers Art. 15 (right to access), Art. 20 (portability) and Art. 30
(processing record).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import (
    get_current_user,
    get_db_session,
    get_workspace_id,
)
from backend.api.main import create_app
from backend.api.v1.inside import build_inside_index, build_inside_storage
from backend.config import get_settings
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow, WorkspacesBase
from backend.knowledge.canonicalization import models
from backend.knowledge.canonicalization.index import CanonicalizationIndex
from backend.knowledge.canonicalization.store import NoteStore
from backend.knowledge.graph.storage import FileSystemStorage

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio

#: The directory segment this test's vault is seeded under. The export reads
#: through injected ``build_inside_storage`` / ``build_inside_index``, so this
#: is just a folder name — deliberately NOT the deployment region, so the Art.30
#: assertions below cannot pass by coincidence.
_VAULT_SEGMENT = "eu-1"


@pytest_asyncio.fixture
async def db():
    async with db_engine(WorkspacesBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client_with_ws(db, tmp_path):
    """Client + a workspace + a user with an active membership.

    Also roots the per-workspace knowledge vault at a tmp dir (via the same
    ``build_inside_storage`` / ``build_inside_index`` builders the export reads
    through) and yields a :class:`FileSystemStorage` over it so a test can seed
    real ``concepts/active/<id>.md`` notes the way the concept tests do.
    """
    app = create_app()
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()

    ws_vault = tmp_path / "vault" / _VAULT_SEGMENT / str(workspace_id)
    ws_vault.mkdir(parents=True, exist_ok=True)
    vault_storage = FileSystemStorage(ws_vault)

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    async def _storage() -> FileSystemStorage:
        return FileSystemStorage(ws_vault)

    async def _index() -> CanonicalizationIndex:
        index = CanonicalizationIndex()
        await index.initialize(FileSystemStorage(ws_vault))
        return index

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[build_inside_storage] = _storage
    app.dependency_overrides[build_inside_index] = _index

    async with db() as s:
        s.add(
            WorkspaceRow(
                id=workspace_id,
                name="Acme",
                safe_mode=True,
                legal_basis="contract",
            )
        )
        s.add(UserRow(id=user_id, supabase_user_id="test-user", email="t@example.com"))
        await s.flush()
        s.add(
            MembershipRow(id=uuid.uuid4(), user_id=user_id, workspace_id=workspace_id, role="owner")
        )
        await s.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, workspace_id, user_id, vault_storage


async def test_export_returns_profile_and_workspace(client_with_ws) -> None:
    c, workspace_id, user_id, _vault = client_with_ws
    r = await c.get("/api/v1/workspace/export")
    assert r.status_code == 200, r.text
    body = r.json()
    # Expected top-level shape — stable contract for portability.
    for key in (
        "workspace",
        "profile",
        "products",
        "product_resources",
        "resource_bindings",
        "runs",
        "deliverables",
        "decisions",
        "knowledge_concepts",
        "exported_at",
    ):
        assert key in body, f"missing key {key} in {sorted(body.keys())}"
    assert body["workspace"]["id"] == str(workspace_id)
    assert body["workspace"]["legal_basis"] == "contract"
    assert body["profile"]["user_id"] == str(user_id)
    assert body["profile"]["email"] == "t@example.com"
    assert body["profile"]["membership"]["role"] == "owner"


async def test_export_unauthenticated_rejected(db) -> None:
    """No fake user → 401 from the v1 router-level get_current_user dep."""
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/v1/workspace/export")
        assert r.status_code == 401


async def test_export_empty_workspace_has_no_concepts(client_with_ws) -> None:
    """A workspace with an empty vault exports an empty concept list — not an
    error, and not a stale DB read."""
    c, _workspace_id, _user_id, _vault = client_with_ws
    r = await c.get("/api/v1/workspace/export")
    assert r.status_code == 200, r.text
    assert r.json()["knowledge_concepts"] == []


async def test_export_carries_real_vault_concepts(client_with_ws) -> None:
    """The export's ``knowledge_concepts`` reflects the founder's REAL active
    concepts in the vault (``concepts/active/<id>.md``) — the same FS-as-SoT
    ``GET /inside/concepts`` + the knowledge graph render.

    RED before the fix: the export read the producer-less ``canonical_anchors``
    DB table, so ``knowledge_concepts`` was ``[]`` for every workspace despite a
    populated vault — a GDPR Art. 15/20 under-report.
    """
    c, _workspace_id, _user_id, vault = client_with_ws

    # Seed a real active concept the way the concept tests do (drive the
    # production NoteStore, not a hand-rolled DB row).
    store = NoteStore(vault)
    await store.write_concept(
        models.ConceptEntry(
            concept_id="jwks-rotation",
            path="concepts/active/jwks-rotation.md",
            display="JWKS rotation",
            aliases=["key rotation", "jwk rollover"],
            created_at=datetime(2026, 6, 14, tzinfo=UTC),
            updated_at=datetime(2026, 6, 15, tzinfo=UTC),
            note_type="TechInsight",
        ),
        initial_body="Rotate signing keys without dropping in-flight tokens.",
    )

    r = await c.get("/api/v1/workspace/export")
    assert r.status_code == 200, r.text
    concepts = r.json()["knowledge_concepts"]
    ids = {row["id"] for row in concepts}
    assert "jwks-rotation" in ids, f"vault concept missing from export: {concepts}"
    row = next(row for row in concepts if row["id"] == "jwks-rotation")
    assert row["name"] == "JWKS rotation"
    assert row["type"] == "TechInsight"
    assert row["aliases"] == ["key rotation", "jwk rollover"]
    assert "Rotate signing keys" in row["description"]
    assert row["created_at"].startswith("2026-06-14")
    assert row["updated_at"].startswith("2026-06-15")


async def test_processing_record_returns_art30_doc(client_with_ws) -> None:
    c, workspace_id, _, _vault = client_with_ws
    r = await c.get("/api/v1/workspace/processing-record")
    assert r.status_code == 200, r.text
    body = r.json()
    # Art. 30 minimum required fields.
    for key in (
        "controller",
        "purposes",
        "categories_of_data",
        "categories_of_recipients",
        "sub_processors",
        "retention",
        "security_measures",
        "legal_basis",
        "workspace_id",
        "region",
        "generated_at",
    ):
        assert key in body, f"missing Art. 30 key {key} in {sorted(body.keys())}"
    assert body["workspace_id"] == str(workspace_id)
    # The region where processing ACTUALLY happens — a deployment constant.
    # There used to be a per-workspace ``region`` column and this record echoed
    # it, asserting a residency control the system does not implement. The
    # column is gone; the record must name the deployment.
    assert body["region"] == get_settings().knowledge_default_region
    assert body["region"] != _VAULT_SEGMENT, (
        "Art. 30 record named a directory segment instead of the deployment region"
    )
    assert body["legal_basis"] == "contract"
    assert isinstance(body["sub_processors"], list)
    # The sub-processor list must include the obvious ones.
    names = [sp["name"].lower() for sp in body["sub_processors"]]
    assert any("supabase" in n for n in names)


#: The window the "founder set a retention window" test PATCHes in. Deliberately
#: NOT 30 — the ``workspaces`` retention sentence already says "retained 30 days
#: then hard-purged", so asserting "30" against the whole record passes without
#: the audit sentence ever having been rewritten.
_PATCHED_RETENTION_DAYS = 17


async def test_processing_record_retention_names_the_trail_that_exists(
    client_with_ws,
) -> None:
    """Art. 30 retention must describe the audit trail the system actually keeps.

    ``audit_events`` is producer-less: nothing constructs the ORM row, nothing
    selects it, nothing deletes it. Prod measured 0 rows on 2026-08-16 and again
    on 2026-09-09 — 0 inserts in the same stats window that took 687 into
    ``audit_outbox`` (5,494 live). Naming it here asserted a retention control
    over a table that has never held a byte.

    The "1 year" window was a bare string too. Retention is the per-workspace
    ``audit_retention_days`` knob swept daily against ``audit_outbox``; its
    documented default is NULL = forever, and prod runs 3/3 workspaces on NULL.
    ``365`` appears nowhere in the retention path. So the record promised a
    deletion the system does not perform.

    This is the THIRD defect of that shape in this one record — ``region``
    echoed a column that never steered anything, and ``_build_export`` read
    ``canonical_anchors`` ("producer-less: nothing writes it"). Both got rich
    assertions once found. ``retention`` survived because the Art. 30 test above
    only asserts the key is PRESENT and never opens it.
    """
    c, _workspace_id, _, _vault = client_with_ws
    r = await c.get("/api/v1/workspace/processing-record")
    assert r.status_code == 200, r.text
    retention = r.json()["retention"]

    # Negative control — a producer-less table must not be the subject of a
    # retention promise anywhere in the record.
    assert "audit_events" not in " ".join(f"{k} {v}" for k, v in retention.items()), (
        "Art. 30 retention names ``audit_events``, a table nothing writes"
    )
    # The trail that does exist has to be the one named. Assert on THIS entry
    # rather than the whole record: the sibling sentences talk about retention
    # windows too, and would satisfy a record-wide match on their own.
    assert "audit_outbox" in retention, (
        f"Art. 30 retention does not cover ``audit_outbox``, the table that "
        f"holds the audit trail — keys: {sorted(retention)}"
    )
    audit_sentence = retention["audit_outbox"]
    assert "audit_retention_days" in audit_sentence, (
        "Art. 30 states a window no code enforces instead of the "
        "``audit_retention_days`` knob that does"
    )
    assert "forever" in audit_sentence.lower(), (
        "Art. 30 omits the NULL = forever default this workspace runs on, so it "
        "promises a deletion the system does not perform"
    )


async def test_processing_record_retention_states_the_window_in_force(
    client_with_ws,
) -> None:
    """Setting a window must move the Art. 30 record, not just the sweep.

    The default branch above is what prod runs (3/3 workspaces NULL), so it is
    the one a record drifts on unnoticed. This walks the other branch: a founder
    PATCHes ``audit_retention_days`` — the same column the daily sweep selects
    on — and the record must state the window now in force and stop saying rows
    are kept forever.

    Echoing this column is the opposite of the ``region`` defect this record
    already carries a comment about: ``region`` steered nothing, whereas
    :mod:`plugin.audit.retention_sweep` reads exactly this value.
    """
    c, _workspace_id, _, _vault = client_with_ws

    patched = await c.patch(
        "/api/v1/workspace",
        json={"audit_retention_days": _PATCHED_RETENTION_DAYS},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["audit_retention_days"] == _PATCHED_RETENTION_DAYS

    r = await c.get("/api/v1/workspace/processing-record")
    assert r.status_code == 200, r.text
    audit_sentence = r.json()["retention"]["audit_outbox"]

    assert str(_PATCHED_RETENTION_DAYS) in audit_sentence, (
        f"Art. 30 does not state the {_PATCHED_RETENTION_DAYS}-day window the "
        f"founder set, so it describes a control other than the one in force: "
        f"{audit_sentence!r}"
    )
    assert "forever" not in audit_sentence.lower(), (
        "Art. 30 still claims rows are kept forever after a window was set — "
        "the record is not reading the column the sweep reads"
    )
