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
from backend.workflow.infrastructure.db import (
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


async def _seed_run(
    s,
    *,
    run_id: uuid.UUID,
    ws: uuid.UUID,
    payload: dict | None = None,
    product_id: uuid.UUID | None = None,
) -> None:
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
            product_id=product_id,
            status=RunStatus.SHIPPED,
            payload=payload or {},
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
        await _seed_run(
            s,
            run_id=run_id,
            ws=workspace_id,
            payload={"intent_text": "Add a getRelatedPosts helper to blog.ts"},
        )
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

    # The report carries the founder's Direction (from the producing run's
    # payload) so it reads as a document: request → built → checked.
    assert body["request"] == "Add a getRelatedPosts helper to blog.ts"

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
    # The seed run carries no intent_text → request degrades to null, not a 500.
    assert body["request"] is None


async def test_report_surfaces_referenced_knowledge(configured_client, db, workspace_id) -> None:
    """G2: the knowledge the agent referenced (canon / prior decisions / prior
    rejections folded into the verify contract) surfaces as a first-class
    ``references`` list — the "근거 포함 답변" the founder reads as "what BSVibe
    referenced", distinct from the verification checklist."""
    from backend.workflow.application.verification_service import RETRIEVED_KNOWLEDGE_RATIONALE

    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    contract = {
        "checks": [
            {"kind": "command", "command": "pytest -q", "rationale": "tests pass"},
            {"kind": "judge", "criteria": ["reads cleanly"], "rationale": "style"},
            {
                "kind": "judge",
                "criteria": [
                    "Avoid (prior rejection) — never ship a payment change without a regression test",
                    "Prior decision — Q: Which database? A: Use Postgres",
                ],
                "rationale": RETRIEVED_KNOWLEDGE_RATIONALE,
            },
        ]
    }
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, payload={"intent_text": "payments"})
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "x"},
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
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    references = r.json()["references"]
    assert references == [
        "Avoid (prior rejection) — never ship a payment change without a regression test",
        "Prior decision — Q: Which database? A: Use Postgres",
    ]
    # The non-knowledge judge check's criteria ("reads cleanly") must NOT leak
    # into references — only the retrieved-knowledge check counts.
    assert "reads cleanly" not in references


async def test_report_references_extracts_legacy_bsage_marker(
    configured_client, db, workspace_id
) -> None:
    """Back-compat: pre-2026-06 deliverables stamped the retrieved-knowledge
    checks with the old "BSage canonical patterns…" wording. After the rename
    (BSage removed from the user-facing string) the references section must STILL
    extract from those historical rows via the legacy marker."""
    from backend.workflow.application.verification_service import (
        LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE,
    )

    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    contract = {
        "checks": [
            {
                "kind": "judge",
                "criteria": ["Prior decision — Q: Which database? A: Use Postgres"],
                "rationale": LEGACY_RETRIEVED_KNOWLEDGE_RATIONALE,
            },
        ]
    }
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id, payload={"intent_text": "x"})
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={"summary": "x"},
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
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    assert r.json()["references"] == ["Prior decision — Q: Which database? A: Use Postgres"]


async def test_report_references_deduped_across_verifications(
    configured_client, db, workspace_id
) -> None:
    """A run may record several verifications (re-attempts); the same referenced
    statement must appear once, in first-seen order."""
    from backend.workflow.application.verification_service import RETRIEVED_KNOWLEDGE_RATIONALE

    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()

    def _contract(criteria: list[str]) -> dict:
        return {
            "checks": [
                {"kind": "judge", "criteria": criteria, "rationale": RETRIEVED_KNOWLEDGE_RATIONALE}
            ]
        }

    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add_all(
            [
                VerificationResult(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    work_step_id=None,
                    workspace_id=workspace_id,
                    outcome=VerificationOutcome.FAILED,
                    contract=_contract(["pattern A", "pattern B"]),
                    result={},
                    created_at=datetime.now(tz=UTC),
                ),
                VerificationResult(
                    id=uuid.uuid4(),
                    run_id=run_id,
                    work_step_id=None,
                    workspace_id=workspace_id,
                    outcome=VerificationOutcome.PASSED,
                    contract=_contract(["pattern B", "pattern C"]),
                    result={},
                    created_at=datetime.now(tz=UTC) + timedelta(seconds=1),
                ),
            ]
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    assert r.json()["references"] == ["pattern A", "pattern B", "pattern C"]


async def test_report_references_empty_without_retrieved_knowledge(
    configured_client, db, workspace_id
) -> None:
    """A verification with only command / non-knowledge judge checks yields an
    empty references list — never a fabricated reference."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.PR,
                payload={},
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
                contract={"checks": [{"kind": "command", "command": "pytest", "rationale": "t"}]},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    assert r.json()["references"] == []


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


# ── B4: the "verified" signal MUST be backed by a PASSED VerificationResult ──
# Defense-in-depth on the READ path. B2b gates the source (a verified Deliverable
# is only written on a PASSED VerificationResult), but the founder-facing
# "verified" signal must DERIVE from a real PASSED row — never be inferred from a
# Deliverable merely existing. A hollow Deliverable (no PASSED row) reads
# unverified, honestly.


async def test_report_verified_true_with_passed_verification(
    configured_client, db, workspace_id
) -> None:
    """A run WITH a PASSED VerificationResult → the report's ``verified`` is True."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "ok", "artifact_refs": ["src/a.py"]},
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
                contract={},
                result={},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True
    assert body["deliverable"]["verified"] is True


async def test_report_verified_false_for_hollow_deliverable(
    configured_client, db, workspace_id
) -> None:
    """The DEFENSIVE delta: a Deliverable that exists with NO PASSED
    VerificationResult (the hollow case constructed directly) must read
    ``verified=False`` — the report shows it as needs-review, never verified."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "hollow", "artifact_refs": []},
                created_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/report")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is False
    assert body["deliverable"]["verified"] is False


async def test_report_verified_false_when_only_failed_verification(
    configured_client, db, workspace_id
) -> None:
    """A Deliverable whose run has only a FAILED VerificationResult is NOT
    verified — a non-PASSED row must never be inferred as proof."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_run(s, run_id=run_id, ws=workspace_id)
        s.add(
            Deliverable(
                id=deliverable_id,
                run_id=run_id,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "failed", "artifact_refs": []},
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=run_id,
                work_step_id=None,
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
    assert r.json()["verified"] is False


async def test_list_and_get_carry_verified_flag(configured_client, db, workspace_id) -> None:
    """The list + single-deliverable surfaces carry a ``verified`` flag derived
    from a PASSED VerificationResult, so the PWA "This is verified" badge renders
    from a backend-authoritative flag rather than deliverable existence alone."""
    verified_run = uuid.uuid4()
    hollow_run = uuid.uuid4()
    verified_id = uuid.uuid4()
    hollow_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        await _seed_run(s, run_id=verified_run, ws=workspace_id)
        await _seed_run(s, run_id=hollow_run, ws=workspace_id)
        s.add(
            Deliverable(
                id=verified_id,
                run_id=verified_run,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "real"},
                created_at=base + timedelta(minutes=5),
            )
        )
        s.add(
            Deliverable(
                id=hollow_id,
                run_id=hollow_run,
                workspace_id=workspace_id,
                deliverable_type=DeliverableType.CODE,
                payload={"summary": "hollow"},
                created_at=base,
            )
        )
        s.add(
            VerificationResult(
                id=uuid.uuid4(),
                run_id=verified_run,
                work_step_id=None,
                workspace_id=workspace_id,
                outcome=VerificationOutcome.PASSED,
                contract={},
                result={},
                created_at=base,
            )
        )
        await s.commit()

    r = await configured_client.get("/api/v1/deliverables")
    assert r.status_code == 200, r.text
    by_id = {row["id"]: row for row in r.json()}
    assert by_id[str(verified_id)]["verified"] is True
    assert by_id[str(hollow_id)]["verified"] is False

    r2 = await configured_client.get(f"/api/v1/deliverables/{hollow_id}")
    assert r2.status_code == 200, r2.text
    assert r2.json()["verified"] is False

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
    product_id: uuid.UUID | None = None,
) -> None:
    """Seed the parent run (flushed first for the PG FK) + a deliverable whose
    payload carries ``artifact_refs``."""
    await _seed_run(s, run_id=run_id, ws=ws, product_id=product_id)
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


@pytest.fixture
def product_workspace_root(tmp_path: Path, monkeypatch) -> Path:
    """Point ``product_workspace_root`` at a tmp dir + clear the settings cache,
    mirroring :func:`run_workspace_root`. W1/W2 ship-time merge persists the
    produced files under ``<product_workspace_root>/<product_id>/`` (the product
    repo's main checkout)."""
    root = tmp_path / "products"
    root.mkdir()
    monkeypatch.setenv("BSVIBE_PRODUCT_WORKSPACE_ROOT", str(root))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _write_product_file(root: Path, product_id: uuid.UUID, ref: str, content: str) -> Path:
    path = root / str(product_id) / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def test_artifact_falls_back_to_product_main_when_run_dir_cleaned(
    configured_client, db, workspace_id, run_workspace_root, product_workspace_root
) -> None:
    """W3 dogfood (2026-05-28): after W2 auto-ship merges the run worktree to
    the product's main and REMOVES the run worktree, the produced file lives
    only under ``<product_workspace_root>/<product_id>/`` — the run dir is gone.
    The artifact endpoint must fall back to the product main checkout for a
    product-bound run instead of 404ing, or the Files viewer can never open a
    shipped product run's files."""
    run_id = uuid.uuid4()
    product_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s,
            deliverable_id=deliverable_id,
            run_id=run_id,
            ws=workspace_id,
            refs=["mathbox.py"],
            product_id=product_id,
        )
    # Run dir intentionally NOT written (auto-ship cleaned it). The file lives
    # in the product workspace main checkout.
    _write_product_file(
        product_workspace_root, product_id, "mathbox.py", "def subtract(a, b):\n    return a - b\n"
    )

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/artifacts/mathbox.py")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ref"] == "mathbox.py"
    assert body["content"] == "def subtract(a, b):\n    return a - b\n"
    assert body["binary"] is False


async def test_artifact_non_product_run_still_404_when_run_dir_cleaned(
    configured_client, db, workspace_id, run_workspace_root, product_workspace_root
) -> None:
    """A run with NO product_id has no product main to fall back to — a cleaned
    run dir stays a calm 404 (no cross-product leak, no 500)."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_refs(
            s, deliverable_id=deliverable_id, run_id=run_id, ws=workspace_id, refs=["gone.py"]
        )

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


# ---------------------------------------------------------------------------
# Captured diff — GET /{id}/diff (Lift 2a)
# ---------------------------------------------------------------------------


async def _seed_deliverable_with_payload(
    s, *, deliverable_id: uuid.UUID, run_id: uuid.UUID, ws: uuid.UUID, payload: dict
) -> None:
    await _seed_run(s, run_id=run_id, ws=ws)
    s.add(
        Deliverable(
            id=deliverable_id,
            run_id=run_id,
            workspace_id=ws,
            deliverable_type=DeliverableType.CODE,
            payload=payload,
            created_at=datetime.now(tz=UTC),
        )
    )
    await s.commit()


async def test_diff_returns_stored_unified_diff(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    unified = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    async with db() as s:
        await _seed_deliverable_with_payload(
            s,
            deliverable_id=deliverable_id,
            run_id=run_id,
            ws=workspace_id,
            payload={"artifact_refs": ["foo.py"], "summary": "x", "diff": unified},
        )

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diff"] == unified
    assert body["truncated"] is False


async def test_diff_truncated_flag_surfaced(configured_client, db, workspace_id) -> None:
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_payload(
            s,
            deliverable_id=deliverable_id,
            run_id=run_id,
            ws=workspace_id,
            payload={"diff": "partial…", "diff_truncated": True},
        )

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/diff")
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] is True


async def test_diff_null_when_no_captured_diff(configured_client, db, workspace_id) -> None:
    """A deliverable with no captured diff (Direct run / old row) returns a calm
    null diff rather than 404 — the front end falls back to additions."""
    run_id = uuid.uuid4()
    deliverable_id = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_payload(
            s,
            deliverable_id=deliverable_id,
            run_id=run_id,
            ws=workspace_id,
            payload={"artifact_refs": ["note.md"], "summary": "x"},
        )

    r = await configured_client.get(f"/api/v1/deliverables/{deliverable_id}/diff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["diff"] is None
    assert body["truncated"] is False


async def test_diff_cross_workspace_404(configured_client, db, workspace_id) -> None:
    other_ws = uuid.uuid4()
    other_run = uuid.uuid4()
    theirs = uuid.uuid4()
    async with db() as s:
        await _seed_deliverable_with_payload(
            s,
            deliverable_id=theirs,
            run_id=other_run,
            ws=other_ws,
            payload={"diff": "secret diff"},
        )

    r = await configured_client.get(f"/api/v1/deliverables/{theirs}/diff")
    assert r.status_code == 404
