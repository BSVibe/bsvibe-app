"""What the founder sends a RUNNING run reaches it — and is not written over by the drive.

Prod #1293 (run 94e04ab3, 2026-11-03): two issue edits were routed to the run
(``issue_edit_routed_to_run``, the second even refreshed the ledger), and both were gone by
the verdict — the run implemented the body as first filed. Intake wrote ``run.payload``
while the drive held its own copy of the run and wrote that back over it; and the drive
only read routed edits when a drive STARTED, so a mid-drive edit was never told anyway.

Now intake never writes the payload of a run: it appends a row to the run's inbox (an
``execution_run_activities`` row, insert-only), and the drive — the payload's one writer —
applies what is new at its start and between every turn.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application.intake.issue_edit import (
    route_issue_comment_to_run,
    route_issue_edit_to_run,
)
from backend.workflow.application.run_inbox import apply_inbox
from backend.workflow.domain.requirements import REQUIREMENTS_KEY
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from plugin.github.webhook import parse_webhook
from tests._support import db_engine

from . import test_issue_edit_routing as edits

pytestmark = pytest.mark.asyncio

_COMMENT = "하나 더:\n- [ ] 통과율이 0 인 제품도 문구가 깨지지 않는다\n"


def _comment(text: str = _COMMENT, *, association: str = "OWNER") -> dict[str, Any]:
    raw = json.dumps(
        {
            "action": "created",
            "issue": {"number": 1263, "title": "t", "body": edits._BODY_V1},
            "comment": {
                "body": text,
                "user": {"login": "blasin", "type": "User"},
                "author_association": association,
            },
            "repository": {"full_name": edits.REPO},
            "sender": {"login": "blasin", "type": "User"},
        }
    ).encode()
    trig = parse_webhook(
        workspace_id=uuid.uuid4(),
        headers={"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": uuid.uuid4().hex},
        raw_body=raw,
    )
    assert trig is not None
    return dict(trig.payload)


def _live(run: ExecutionRun) -> list[str]:
    return [i["text"] for i in run.payload[REQUIREMENTS_KEY] if not i.get("withdrawn")]


async def test_an_edit_survives_the_drive_writing_its_stale_copy_back() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING)
        async with sf() as drive, workspace_session_scope(drive, ws):
            run = await drive.get(ExecutionRun, run_id)  # the drive's copy, read first
            assert run is not None
            async with sf() as intake:
                assert (
                    await route_issue_edit_to_run(
                        intake, workspace_id=ws, payload=edits._event(edits._BODY_V2)
                    )
                    == run_id
                )
                await intake.commit()
            run.payload = {**run.payload, "turns": 1}  # the drive writes ITS copy back
            await drive.commit()
            told = await apply_inbox(drive, run)
            await drive.commit()
        run = await edits._run(sf, ws, run_id)

    assert "항상 저장한다" in run.payload["intent_text"]
    assert "성공한 action 의 보상 핸들은 항상 저장한다" in _live(run)
    assert "실패한 배달의 보상 핸들은 저장하지 않는다" not in _live(run)
    assert run.payload["turns"] == 1  # the drive's own write stands too
    [message] = told
    assert "edited the issue" in message["content"]


async def test_the_inbox_is_told_once() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING)
        await edits._route(sf, ws, edits._event(edits._BODY_V2))
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            first = await apply_inbox(s, run)
            second = await apply_inbox(s, run)
            await s.commit()

    assert len(first) == 1 and second == []


async def test_intake_never_writes_the_payload_of_a_running_run() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING)
        before = (await edits._run(sf, ws, run_id)).payload
        await edits._route(sf, ws, edits._event(edits._BODY_V2))
        async with sf() as s:
            await route_issue_comment_to_run(s, workspace_id=ws, payload=_comment())
            await s.commit()
        after = (await edits._run(sf, ws, run_id)).payload

    assert after == before


async def test_a_comment_reaches_the_running_run_and_its_items_join_the_ledger() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING)
        async with sf() as s:
            routed = await route_issue_comment_to_run(s, workspace_id=ws, payload=_comment())
            await s.commit()
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            [message] = await apply_inbox(s, run)
            await s.commit()
        run = await edits._run(sf, ws, run_id)

    assert routed == run_id
    added = [i for i in run.payload[REQUIREMENTS_KEY] if i["text"].startswith("통과율이 0")]
    assert [i["id"] for i in added] == ["R4"]
    assert not any(i.get("withdrawn") for i in run.payload[REQUIREMENTS_KEY])
    assert "commented on the issue" in message["content"]


async def test_a_comment_reopens_a_run_waiting_for_review_and_a_stranger_is_not_routed() -> None:
    async def admin(workspace_id: uuid.UUID, repo: str, login: str) -> str:
        return "admin"

    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await edits._seed(sf, status=RunStatus.REVIEW_READY)
        async with sf() as s:
            assert (
                await route_issue_comment_to_run(
                    s, workspace_id=ws, payload=_comment(association="NONE")
                )
                is None
            )
            assert (
                await route_issue_comment_to_run(
                    s,
                    workspace_id=ws,
                    payload=_comment(association="CONTRIBUTOR"),
                    permission_of=admin,
                )
                == run_id
            )
            await s.commit()
        run = await edits._run(sf, ws, run_id)

    assert run.status is RunStatus.OPEN


async def test_the_intake_worker_routes_the_comment_instead_of_queueing_a_new_run() -> None:
    """Through the real ``IntakeWorker.drain_once``, with the founder seen as CONTRIBUTOR."""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from backend.connectors.db import ConnectorAccountRow
    from backend.data.rls import workspace_session_scope
    from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository
    from backend.identity.workspaces_db import ProductRow, WorkspaceRow
    from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
    from backend.workflow.infrastructure.workers.intake_worker import (
        IntakeWorker,
        IntakeWorkerConfig,
    )

    async def admin(workspace_id: uuid.UUID, repo: str, login: str) -> str:
        return "admin"

    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, product_id, account_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(WorkspaceRow(id=ws, name="ws", safe_mode=True))
            await s.flush()
            s.add(ProductRow(id=product_id, workspace_id=ws, name="App", slug="app"))
            s.add(
                ConnectorAccountRow(
                    id=account_id,
                    workspace_id=ws,
                    connector="github",
                    webhook_token=f"tok-{uuid.uuid4().hex}",
                    signing_secret_ciphertext="cipher",
                )
            )
            await s.flush()
            await SqlAlchemyResourceBindingRepository(s).create(
                workspace_id=ws,
                product_id=product_id,
                connector_account_id=account_id,
                resource_id=edits.REPO,
                trigger={"filters": {}},
            )
            await s.commit()
        _ws, run_id = await edits._seed(sf, status=RunStatus.RUNNING, workspace_id=ws)
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(
                TriggerEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    source="github",
                    trigger_kind=TriggerKind.WEBHOOK,
                    idempotency_key=f"k-{uuid.uuid4().hex}",
                    payload={
                        **_comment(association="CONTRIBUTOR"),
                        "connector_account_id": str(account_id),
                        "resource_id": edits.REPO,
                    },
                    received_at=datetime.now(UTC),
                )
            )
            await s.commit()
        await IntakeWorker(
            session_factory=sf, config=IntakeWorkerConfig(), repo_permission=admin
        ).drain_once()
        async with sf() as s, workspace_session_scope(s, ws):
            requests = list((await s.execute(select(RequestRow))).scalars().all())
        run, _told = await edits._applied(sf, ws, run_id)

    assert len(requests) == 1  # only the run's own: no second run for the issue
    assert any(
        i["text"] == "통과율이 0 인 제품도 문구가 깨지지 않는다"
        for i in run.payload[REQUIREMENTS_KEY]
    )
