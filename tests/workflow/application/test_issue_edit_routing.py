"""An edit to an issue whose run is still in flight goes to THAT run, with its ledger refreshed (#1268).

Before: ``issues.edited`` always became a new request — a second run (and a second PR) for the
same issue, while the first kept the body it started from. Prod #1263 (2026-10-29): R3 was
wrong, the issue was corrected, and the run's ledger kept the old R3 — the run deferred it and
the PR said ``Refs``.

Now an edit by a trusted author reaches the run that issue started: its intent and ledger are
refreshed (same item → same id, removed item → ``withdrawn``, new item → new id), and its next
drive is told. A finished run, a stranger's edit, or an issue with no run: a new request, as
before. Payloads come from the real ``parse_webhook``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application.intake.issue_edit import (
    issue_edit_messages,
    route_issue_edit_to_run,
)
from backend.workflow.domain.requirements import (
    REQUIREMENTS_KEY,
    build_ledger,
    check_ledger,
    ledger_closes,
    ledger_passed,
    refresh_ledger,
    unevidenced,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.intake.db import (
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from plugin.github.webhook import parse_webhook
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

REPO = "acme/app"

_BODY_V1 = """## 할 것
- [ ] 백엔드 경로를 추가한다
- [ ] 실패한 배달의 보상 핸들은 저장하지 않는다
## 가드
- [ ] 다른 워크스페이스는 404
"""
_BODY_V2 = """## 할 것
- [ ] 백엔드 경로를 추가한다
- [ ] 성공한 action 의 보상 핸들은 항상 저장한다
## 가드
- [ ] 다른 워크스페이스는 404
"""


def _event(
    body: str,
    *,
    action: str = "edited",
    number: int = 1263,
    sender: str = "blasin",
    author: str = "blasin",
    association: str = "OWNER",
    pull_request: bool = False,
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "number": number,
        "title": "배달 실패",
        "body": body,
        "user": {"login": author},
        "author_association": association,
    }
    if pull_request:
        issue["pull_request"] = {"url": "x"}
    raw = json.dumps(
        {
            "action": action,
            "issue": issue,
            "repository": {"full_name": REPO},
            "sender": {"login": sender, "type": "User"},
        }
    ).encode()
    trig = parse_webhook(
        workspace_id=uuid.uuid4(),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": uuid.uuid4().hex},
        raw_body=raw,
    )
    assert trig is not None
    return dict(trig.payload)


async def _seed(
    sf: async_sessionmaker, *, status: RunStatus, workspace_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """A run started from issue #1263's ``opened`` delivery, with its ledger recorded."""
    ws, run_id, request_id = workspace_id or uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    opened = _event(_BODY_V1, action="opened")
    trigger_id = uuid.uuid4()
    async with sf() as session, workspace_session_scope(session, ws):
        session.add(
            TriggerEventRow(
                id=trigger_id,
                workspace_id=ws,
                source="github",
                trigger_kind=TriggerKind.WEBHOOK,
                idempotency_key=f"opened-{uuid.uuid4().hex}",
                payload=opened,
                received_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            RequestRow(
                id=request_id,
                workspace_id=ws,
                trigger_event_id=trigger_id,
                payload=opened,
                status=RequestStatus.OPEN,
            )
        )
        await session.flush()
        session.add(
            ExecutionRun(
                id=run_id,
                workspace_id=ws,
                request_id=request_id,
                status=status,
                payload={
                    "intent_text": opened["intent_text"],
                    REQUIREMENTS_KEY: build_ledger(opened),
                },
            )
        )
        await session.commit()
    return ws, run_id


async def _route(sf: async_sessionmaker, ws: uuid.UUID, payload: dict[str, Any]) -> Any:
    async with sf() as session:
        routed = await route_issue_edit_to_run(session, workspace_id=ws, payload=payload)
        await session.commit()
    return routed


async def _run(sf: async_sessionmaker, ws: uuid.UUID, run_id: uuid.UUID) -> ExecutionRun:
    async with sf() as session, workspace_session_scope(session, ws):
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        return run


# ── the ledger refresh (pure) ────────────────────────────────────────────────


def test_a_refreshed_ledger_keeps_ids_withdraws_the_removed_and_numbers_the_new() -> None:
    old = build_ledger(_event(_BODY_V1, action="opened"))
    new = refresh_ledger(old, build_ledger(_event(_BODY_V2)))
    by_text = {i["text"]: i for i in new}

    assert by_text["백엔드 경로를 추가한다"]["id"] == "R1"
    assert by_text["다른 워크스페이스는 404"]["id"] == "R3"
    withdrawn = by_text["실패한 배달의 보상 핸들은 저장하지 않는다"]
    assert withdrawn["id"] == "R2" and withdrawn.get("withdrawn") is True
    assert by_text["성공한 action 의 보상 핸들은 항상 저장한다"]["id"] == "R4"


def test_a_withdrawn_item_is_owed_nothing_and_does_not_hold_the_issue_open() -> None:
    old = build_ledger(_event(_BODY_V1, action="opened"))
    ledger = refresh_ledger(old, build_ledger(_event(_BODY_V2)))
    evidence = [
        {"requirement_id": rid, "status": "done", "paths": ["a.py"]} for rid in ("R1", "R3", "R4")
    ]
    from backend.workflow.domain.requirements import GateFacts

    rows = check_ledger(
        ledger, evidence, GateFacts(diff_paths=frozenset({"a.py"}), passed_commands=())
    )
    for row in rows:
        if row["status"] == "pending":
            row["status"] = "done"  # what the judge would say
    assert {i["id"] for i in unevidenced(ledger, evidence)} == set()
    assert next(r for r in rows if r["id"] == "R2")["status"] == "withdrawn"
    assert ledger_passed(rows) and ledger_closes(rows)


# ── routing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [RunStatus.OPEN, RunStatus.RUNNING])
async def test_an_edit_goes_to_the_in_flight_run(status: RunStatus) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed(sf, status=status)
        routed = await _route(sf, ws, _event(_BODY_V2))
        run = await _run(sf, ws, run_id)

    assert routed == run_id
    assert run.status is status
    texts = {i["text"]: i for i in run.payload[REQUIREMENTS_KEY]}
    assert "성공한 action 의 보상 핸들은 항상 저장한다" in texts
    assert texts["실패한 배달의 보상 핸들은 저장하지 않는다"].get("withdrawn") is True
    assert "항상 저장한다" in run.payload["intent_text"]


@pytest.mark.parametrize("status", [RunStatus.REVIEW_READY, RunStatus.FAILED])
async def test_an_edit_reopens_a_run_waiting_for_review(status: RunStatus) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed(sf, status=status)
        routed = await _route(sf, ws, _event(_BODY_V2))
        run = await _run(sf, ws, run_id)

    assert routed == run_id
    assert run.status is RunStatus.OPEN


@pytest.mark.parametrize("status", [RunStatus.SHIPPED, RunStatus.CANCELLED])
async def test_a_finished_run_is_not_revived(status: RunStatus) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed(sf, status=status)
        routed = await _route(sf, ws, _event(_BODY_V2))
        run = await _run(sf, ws, run_id)

    assert routed is None  # the caller lands a new request, as before
    assert run.status is status


async def test_a_strangers_edit_is_not_routed() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed(sf, status=RunStatus.RUNNING)
        routed = await _route(
            sf, ws, _event(_BODY_V2, sender="rando", author="rando", association="NONE")
        )
        run = await _run(sf, ws, run_id)

    assert routed is None
    assert "항상 저장한다" not in run.payload["intent_text"]


async def test_an_edit_by_someone_other_than_the_trusted_author_is_not_routed() -> None:
    """The sender's own association is not in an ``issues`` delivery; only the author's is."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, _run_id = await _seed(sf, status=RunStatus.RUNNING)
        routed = await _route(sf, ws, _event(_BODY_V2, sender="someone-else"))

    assert routed is None


async def test_another_issue_and_other_events_are_not_routed() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, _run_id = await _seed(sf, status=RunStatus.RUNNING)
        assert await _route(sf, ws, _event(_BODY_V2, number=9999)) is None
        assert await _route(sf, ws, _event(_BODY_V2, action="reopened")) is None


# ── the next drive hears it ──────────────────────────────────────────────────


async def test_the_next_drive_is_told_once() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed(sf, status=RunStatus.RUNNING)
        await _route(sf, ws, _event(_BODY_V2))
        run = await _run(sf, ws, run_id)

    [message] = issue_edit_messages(run)
    assert "edited the issue" in message["content"]
    assert "항상 저장한다" in message["content"]
    assert "withdrawn" in message["content"]
    assert issue_edit_messages(run) == []  # consumed


async def test_the_intake_worker_routes_the_edit_instead_of_queueing_a_new_run() -> None:
    """Through the real ``IntakeWorker.drain_once``, past the binding filter and the gate."""
    from sqlalchemy import select

    from backend.connectors.db import ConnectorAccountRow
    from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository
    from backend.identity.workspaces_db import ProductRow, WorkspaceRow
    from backend.workflow.infrastructure.workers.intake_worker import (
        IntakeWorker,
        IntakeWorkerConfig,
    )

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
                resource_id=REPO,
                trigger={"filters": {}},
            )
            await s.commit()
        _ws, run_id = await _seed(sf, status=RunStatus.RUNNING, workspace_id=ws)
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(
                TriggerEventRow(
                    id=uuid.uuid4(),
                    workspace_id=ws,
                    source="github",
                    trigger_kind=TriggerKind.WEBHOOK,
                    idempotency_key=f"k-{uuid.uuid4().hex}",
                    payload={
                        **_event(_BODY_V2),
                        "connector_account_id": str(account_id),
                        "resource_id": REPO,
                    },
                    received_at=datetime.now(UTC),
                )
            )
            await s.commit()
        await IntakeWorker(session_factory=sf, config=IntakeWorkerConfig()).drain_once()
        async with sf() as s, workspace_session_scope(s, ws):
            requests = list((await s.execute(select(RequestRow))).scalars().all())
        run = await _run(sf, ws, run_id)

    assert len(requests) == 1  # only the run's own: the edit did not become a second run
    assert "항상 저장한다" in run.payload["intent_text"]


async def test_the_drive_seeds_the_edit_before_the_refreshed_ledger() -> None:
    """``ledger_messages`` is what the drive loop seeds at the start of every drive."""
    from backend.workflow.application._loop_context import ledger_messages

    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = await _seed(sf, status=RunStatus.RUNNING)
        await _route(sf, ws, _event(_BODY_V2))
        run = await _run(sf, ws, run_id)

    first, second = ledger_messages(run)
    assert "edited the issue" in first["content"]
    assert "REQUIREMENTS LEDGER" in second["content"]
    assert "withdrawn" in second["content"]
    assert len(ledger_messages(run)) == 1  # the edit was told once; the ledger stays
