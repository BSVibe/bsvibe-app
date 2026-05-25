"""/api/v1/deliverables — read API end-to-end (SQLite default, real PG on env).

Deliverables are *created* by the agent loop / workers, never via HTTP, so the
surface is read-only. These tests seed ``Deliverable`` rows (and the parent
``ExecutionRun`` the PG-enforced FK requires) and assert list/get behaviour:
newest-first ordering, workspace scoping, payload-field mapping, the optional
``run_id`` filter, the 404 for a cross-workspace id, and the limit cap.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from backend.config import get_settings
from backend.execution.db import (
    Deliverable,
    DeliverableType,
    ExecutionBase,
    ExecutionRun,
    RunStatus,
    VerificationOutcome,
    VerificationResult,
)

from .._support import db_engine, fake_current_user

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine(ExecutionBase) as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def configured_client(db, workspace_id: uuid.UUID):
    app = create_app()

    def _ws() -> uuid.UUID:
        return workspace_id

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = _ws
    app.dependency_overrides[get_db_session] = _session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_run(s, *, run_id: uuid.UUID, ws: uuid.UUID) -> None:
    """Create the parent ExecutionRun so the deliverables FK resolves (PG).

    Flush immediately: there is no ORM ``relationship()`` linking Deliverable to
    ExecutionRun (only a column-level FK), and the deliverables are inserted via
    a batched ``executemany`` — so the parent row must be flushed to the DB
    before the children or PG rejects the FK (SQLite silently tolerates it).
    """
    s.add(
        ExecutionRun(
            id=run_id,
            workspace_id=ws,
            status=RunStatus.SHIPPED,
            payload={},
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )
    await s.flush()


async def test_list_newest_first_with_payload_mapping(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    older_id = uuid.uuid4()
    newer_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=older_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                artifact_uri="https://example.com/pr/1",
                payload={"summary": "first ship", "artifact_refs": ["pr#1"]},
                created_at=base,
            )
        )
        s.add(
            Deliverable(
                id=newer_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PAGE,
                artifact_uri=None,
                payload={"summary": "second ship", "artifact_refs": ["page-a", "page-b"]},
                created_at=base + timedelta(minutes=5),
            )
        )
        await s.commit()

    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == [str(newer_id), str(older_id)]

    newest = rows[0]
    assert newest["run_id"] == str(run_id)
    assert newest["workspace_id"] == str(workspace_id)
    assert newest["deliverable_type"] == "page"
    assert newest["summary"] == "second ship"
    assert newest["artifact_refs"] == ["page-a", "page-b"]
    assert newest["artifact_uri"] is None

    oldest = rows[1]
    assert oldest["summary"] == "first ship"
    assert oldest["artifact_refs"] == ["pr#1"]
    assert oldest["artifact_uri"] == "https://example.com/pr/1"


async def test_list_workspace_scoped(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    mine = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        await _seed_run(s, run_id=other_run_id, ws=other_ws)
        s.add(
            Deliverable(
                id=mine,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        # Another workspace's deliverable — MUST NOT appear.
        s.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=other_run_id,
                workspace_id=other_ws,
                deliverable_type=DeliverableType.CODE,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(mine)


async def test_list_run_id_filter(configured_client, db, workspace_id) -> None:
    run_a = uuid.uuid4()
    run_b = uuid.uuid4()
    in_a = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_a, ws=workspace_id)
        await _seed_run(s, run_id=run_b, ws=workspace_id)
        s.add(
            Deliverable(
                id=in_a,
                run_id=run_a,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "a"},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            Deliverable(
                id=uuid.uuid4(),
                run_id=run_b,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "b"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables?run_id={run_a}")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(in_a)
    assert rows[0]["run_id"] == str(run_a)


async def test_get_by_id_and_cross_workspace_404(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        await _seed_run(s, run_id=other_run_id, ws=other_ws)
        s.add(
            Deliverable(
                id=mine,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "mine", "artifact_refs": []},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            Deliverable(
                id=theirs,
                run_id=other_run_id,
                workspace_id=other_ws,
                deliverable_type=DeliverableType.PR,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{mine}")
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "mine"

    # Cross-workspace id resolves to 404, not a leak.
    r2 = await configured_client.get(f"/api/v1/deliverables/{theirs}")
    assert r2.status_code == 404

    # Unknown id → 404.
    r3 = await configured_client.get(f"/api/v1/deliverables/{uuid.uuid4()}")
    assert r3.status_code == 404


async def test_list_empty(configured_client) -> None:
    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200
    assert r.json() == []


async def test_report_returns_deliverable_with_verification(
    configured_client, db, workspace_id
) -> None:
    """The report bundles the deliverable + the VerificationResult rows for its
    run — each carrying outcome / contract / result, the "how BSVibe checked
    this" proof."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    contract = {
        "checks": [
            {"kind": "command", "command": "pytest -q", "rationale": "tests pass"},
            {"kind": "judge", "criteria": ["reads cleanly"], "rationale": "style"},
        ]
    }
    result = {"checks": [{"passed": True, "output": "19 passed"}]}
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                artifact_uri="https://github.com/acme/repo/pull/15",
                diff_url="https://github.com/acme/repo/commit/abc",
                payload={"summary": "Add getRelatedPosts", "artifact_refs": ["src/posts.ts"]},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                work_step_id=None,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract=contract,
                result=result,
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()

    d = body["deliverable"]
    assert d["id"] == str(deliverable_id)
    assert d["summary"] == "Add getRelatedPosts"
    assert d["artifact_refs"] == ["src/posts.ts"]
    assert d["artifact_uri"] == "https://github.com/acme/repo/pull/15"
    assert d["diff_url"] == "https://github.com/acme/repo/commit/abc"
    assert d["deliverable_type"] == "pr"

    assert len(body["verifications"]) == 1
    v = body["verifications"][0]
    assert v["outcome"] == "passed"
    assert v["contract"] == contract
    assert v["result"] == result


async def test_report_empty_verification_does_not_error(
    configured_client, db, workspace_id
) -> None:
    """A run with no VerificationResult yields a calm empty list, not a 500."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.DIRECT_OUTPUT,
                payload={"summary": "direct"},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deliverable"]["id"] == str(deliverable_id)
    assert body["verifications"] == []


async def test_report_cross_workspace_404(configured_client, db, workspace_id) -> None:
    """A deliverable in another workspace's report is 404, never a leak."""
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=other_run_id, ws=other_ws)
        s.add(
            Deliverable(
                id=theirs,
                run_id=other_run_id,
                workspace_id=other_ws,
                deliverable_type=DeliverableType.PR,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{theirs}/report")
    assert r.status_code == 404

    r2 = await configured_client.get(f"/api/v1/deliverables/{uuid.uuid4()}/report")
    assert r2.status_code == 404


async def test_report_only_includes_own_run_verifications(
    configured_client, db, workspace_id
) -> None:
    """Verification rows are scoped to the deliverable's run, not all of them."""
    run_id = uuid.uuid4()
    other_run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        await _seed_run(s, run_id=other_run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        # A verification for an unrelated run — MUST NOT appear.
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=other_run_id,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.FAILED,
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    verifications = r.json()["verifications"]
    assert len(verifications) == 1
    assert verifications[0]["outcome"] == "passed"


async def test_limit_capped(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        for _ in range(3):
            s.add(
                Deliverable(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    workspace_id=workspace_id,
                    deliverable_type=DeliverableType.CODE,
                    payload={},
                    created_at=datetime.now(tz=UTC),
                )
            )
        await s.commit()

    # Over-cap and under-floor limits are clamped, not errored.
    r = await configured_client.get("/api/v1/deliverables?limit=99999")
    assert r.status_code == 200, r.text
    assert len(r.json()) == 3

    r2 = await configured_client.get("/api/v1/deliverables?limit=1")
    assert r2.status_code == 200
    assert len(r2.json()) == 1


# ---------------------------------------------------------------------------
# Artifact content viewer — GET /{id}/artifacts/{ref:path}
#
# Serves a deliverable's produced file CONTENT read-only from the persisted run
# workspace (``<run_workspace_root>/<run_id>/<ref>``). The ``run_workspace_root``
# is read from settings, so the fixture points it at a tmp dir + clears the
# ``get_settings`` lru_cache (mirrors tests/api/test_v1_skills.py).
# ---------------------------------------------------------------------------


async def _seed_deliverable_with_refs(
    s,
    *,
    deliverable_id: uuid.UUID,
    run_id: uuid.UUID,
    ws: uuid.UUID,
    refs: list[str],
) -> None:
    """Seed the parent run (flushed first for the PG FK) + a deliverable whose
    payload carries ``artifact_refs``."""
    await _seed_run(s, run_id=run_id, ws=ws)
    s.add(
        Deliverable(
            id=deliverable_id,
            run_id=run_id,
            workspace_id=ws,
            deliverable_type=DeliverableType.CODE,
            payload={"summary": "shipped", "artifact_refs": refs},
            created_at=datetime.now(tz=UTC),
        )
    )
    await s.commit()


@pytest.fixture
def run_workspace_root(tmp_path: Path, monkeypatch) -> Path:
    """Point ``run_workspace_root`` at a tmp dir; clear the settings cache so the
    override takes effect for the request-time ``get_settings()`` read."""
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setenv("BSVIBE_RUN_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _write_run_file(root: Path, run_id: uuid.UUID, ref: str, content: str | bytes) -> Path:
    """Write a file into ``<root>/<run_id>/<ref>`` (creating parents)."""
    path = root / str(run_id) / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


async def test_artifact_serves_text_content(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["hello.py"]
        )
    _write_run_file(run_workspace_root, run_id, "hello.py", "print('hi')\n")

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/hello.py")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ref"] == "hello.py"
    assert body["content"] == "print('hi')\n"
    assert body["truncated"] is False
    assert body["binary"] is False


async def test_artifact_serves_nested_ref(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """A ref with a subdirectory (e.g. ``src/app.py``) is served — the ``:path``
    converter keeps the slash, and the realpath stays inside the run dir."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["src/app.py"]
        )
    _write_run_file(run_workspace_root, run_id, "src/app.py", "x = 1\n")

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/src/app.py")
    assert r.status_code == 200, r.text
    assert r.json()["content"] == "x = 1\n"


async def test_artifact_ref_not_in_whitelist_404(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """A ref that is NOT one of the deliverable's own artifact_refs is rejected,
    even when a file by that name physically exists in the run dir."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["hello.py"]
        )
    # secret.txt exists on disk but is NOT in artifact_refs.
    _write_run_file(run_workspace_root, run_id, "secret.txt", "shh")

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/secret.txt")
    assert r.status_code == 404


async def test_artifact_path_traversal_rejected(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """A traversal ref (``../``) is rejected even if it were somehow whitelisted
    — the resolved realpath must stay within the run dir."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    traversal = "../../etc/passwd"
    async with db() as s:
        await _seed_deliverable_with_refs(
            s,
            deliverable_id=deliverable_id,
            run_id=run_id,
            ws=workspace_id,
            refs=[traversal],  # even whitelisted, must be refused
        )

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/{traversal}")
    assert r.status_code == 404


async def test_artifact_cross_workspace_404(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """A deliverable in another workspace is 404 (never a content leak)."""
    other_run_id = uuid.uuid4()
    other_ws = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=theirs, run_id=other_run_id, ws=other_ws, refs=["hello.py"]
        )
    _write_run_file(run_workspace_root, other_run_id, "hello.py", "print('hi')\n")

    r = await configured_client.get(f"/api/v1/deliverables/{theirs}/artifacts/hello.py")
    assert r.status_code == 404


async def test_artifact_missing_file_404(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """When the run dir was cleaned and the file no longer exists on disk, the
    endpoint 404s calmly (the ref IS whitelisted, but the bytes are gone)."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["gone.py"]
        )
    # No file written → the run dir / file is absent.

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/gone.py")
    assert r.status_code == 404


async def test_artifact_oversized_truncated(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """Content beyond the 256 KiB cap is truncated with ``truncated: true``."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    big = "a" * (256 * 1024 + 500)
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["big.txt"]
        )
    _write_run_file(run_workspace_root, run_id, "big.txt", big)

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/big.txt")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["truncated"] is True
    assert len(body["content"]) == 256 * 1024


async def test_artifact_binary_metadata_only(
    configured_client, db, workspace_id, run_workspace_root
) -> None:
    """A binary file is reported as metadata only, never dumped as bytes."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    raw = b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03"
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["logo.png"]
        )
    _write_run_file(run_workspace_root, run_id, "logo.png", raw)

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/logo.png")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["binary"] is True
    assert "binary file" in body["content"].lower()
    assert str(len(raw)) in body["content"]


async def test_artifact_unknown_deliverable_404(configured_client, run_workspace_root) -> None:
    """An unknown deliverable id is 404, not a 500."""
    r = await configured_client.get(f"/api/v1/deliverables/{uuid.uuid4()}/artifacts/hello.py")
    assert r.status_code == 404
