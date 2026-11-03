"""The run's timeline shows the founder's issue edits / comments that reached its inbox.

Rows are written through the real ``post_to_inbox`` and read back through the real
``/api/v1/runs/{id}/detail`` handler — no hand-built response.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.deps import get_current_user, get_db_session, get_workspace_id
from backend.api.main import create_app
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.run_inbox import (
    ISSUE_COMMENT,
    ISSUE_EDIT,
    post_to_inbox,
)
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus

from .._support import db_engine, fake_current_user, publishing_workspace
from .conftest import commit_per_workspace, flush_per_workspace

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def configured_client(db, workspace_id: uuid.UUID):
    app = create_app()

    async def _session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_current_user] = fake_current_user()
    app.dependency_overrides[get_workspace_id] = publishing_workspace(workspace_id)
    app.dependency_overrides[get_db_session] = _session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed_ordered(db, ws: uuid.UUID, language: str, messages) -> uuid.UUID:
    """A run plus one real inbox row per message, stamped a minute apart in order."""
    run_id = uuid.uuid4()
    base = datetime.now(tz=UTC)
    async with db() as s:
        s.add(WorkspaceRow(id=ws, name="w", language=language))
        run = ExecutionRun(id=run_id, workspace_id=ws, status=RunStatus.RUNNING, payload={})
        s.add(run)
        await flush_per_workspace(s)
        seen: set[uuid.UUID] = set()
        for i, (kind, data) in enumerate(messages):
            await post_to_inbox(s, run, kind, data)
            rows = (
                (
                    await s.execute(
                        select(ExecutionRunActivity).where(ExecutionRunActivity.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
            (fresh,) = [r for r in rows if r.id not in seen]
            seen.add(fresh.id)
            fresh.created_at = base + timedelta(minutes=i)
        await commit_per_workspace(s)
    return run_id


_EDIT = (ISSUE_EDIT, {"issue_number": 1296, "intent_text": "x", "ledger": []})
_COMMENT = (ISSUE_COMMENT, {"issue_number": 1296, "author": "blasin", "text": "hi"})


async def test_edit_and_comment_show_in_order(configured_client, db, workspace_id) -> None:
    run_id = await _seed_ordered(db, workspace_id, "en", [_EDIT, _COMMENT])

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    inbox = [a for a in r.json()["activities"] if a["type"] == "founder_inbox"]
    assert [a["label"] for a in inbox] == [
        "Received an edit to issue #1296",
        "Received @blasin's comment on issue #1296",
    ]


async def test_korean_workspace_reads_korean(configured_client, db, workspace_id) -> None:
    run_id = await _seed_ordered(db, workspace_id, "ko", [_EDIT, _COMMENT])

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    assert [a["label"] for a in r.json()["activities"]] == [
        "이슈 #1296 수정을 받았어요",
        "@blasin 의 이슈 #1296 댓글을 받았어요",
    ]


async def test_odd_payloads_do_not_break_detail(configured_client, db, workspace_id) -> None:
    run_id = await _seed_ordered(
        db,
        workspace_id,
        "ko",
        [("", {}), (ISSUE_EDIT, {}), (ISSUE_COMMENT, {"author": "x"}), _EDIT],
    )

    r = await configured_client.get(f"/api/v1/runs/{run_id}/detail")
    assert r.status_code == 200, r.text
    labels = [a["label"] for a in r.json()["activities"]]
    assert labels[:3] == ["메시지를 받았어요"] * 3
    assert labels[3] == "이슈 #1296 수정을 받았어요"
