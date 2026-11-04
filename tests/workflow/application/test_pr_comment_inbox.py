"""PR 댓글은 런 수신함으로 가고, 드라이브가 반영한다 — 드라이브 중 덮어쓰기·늦은 전달을 막는다.

실제 ``route_pr_comment_to_run`` 과 실제 ``apply_inbox`` 로 돌린다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application.intake.pr_comment import route_pr_comment_to_run
from backend.workflow.application.run_inbox import ISSUE_EDIT, apply_inbox, post_to_inbox
from backend.workflow.domain.pr_review import comment_received_at
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

REPO = "acme/app"
PR = 1218


def _payload(text: str = "fix the lint") -> dict:
    return {
        "github_event": "issue_comment",
        "repo": REPO,
        "body": {
            "issue": {"number": PR, "title": "t", "pull_request": {"url": "x"}},
            "comment": {
                "body": text,
                "author_association": "OWNER",
                "user": {"login": "blasin"},
            },
        },
    }


async def _seed(sf, status: RunStatus = RunStatus.RUNNING) -> tuple[uuid.UUID, uuid.UUID]:  # noqa: ANN001
    run_id, ws = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(ExecutionRun(id=run_id, workspace_id=ws, status=status, payload={"intent_text": "x"}))
        s.add(
            GithubMergeWatchRow(
                workspace_id=ws,
                run_id=run_id,
                deliverable_id=uuid.uuid4(),
                repo=REPO,
                pr_number=PR,
                branch="bsvibe/x",
                status=MergeWatchStatus.PENDING_CI,
                next_poll_at=now,
                deadline_at=now,
            )
        )
        await s.flush()
        await s.commit()
    return run_id, ws


async def _route(sf, ws, text: str = "fix the lint") -> None:  # noqa: ANN001
    async with sf() as s:
        assert await route_pr_comment_to_run(s, workspace_id=ws, payload=_payload(text))
        await s.commit()


async def test_routing_does_not_touch_the_run_payload() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf)
        await _route(sf, ws)
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            assert run.payload == {"intent_text": "x"}


async def test_a_comment_routed_during_a_drive_survives_the_drive_rewriting_its_copy() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf)
        # The drive loads the run once and keeps this copy for the whole drive.
        async with sf() as drive, workspace_session_scope(drive, ws):
            run = await drive.get(ExecutionRun, run_id)
            assert run is not None
            await _route(sf, ws, "rename it")  # a reviewer comments mid-drive
            run.payload = {**run.payload, "drive_wrote": True}  # the drive writes its stale copy
            await drive.flush()
            (message,) = await apply_inbox(drive, run)
            await drive.commit()
        assert "rename it" in message["content"] and f"#{PR}" in message["content"]
        assert "blasin" in message["content"]
        async with sf() as s, workspace_session_scope(s, ws):
            stored = await s.get(ExecutionRun, run_id)
            assert stored is not None
            assert stored.payload["drive_wrote"] is True
            assert [c["body"] for c in stored.payload["pr_comment_record"]] == ["rename it"]


async def test_received_at_is_recorded_after_the_comment_is_applied() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf, RunStatus.FAILED)
        await _route(sf, ws)
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            assert comment_received_at(run.payload) is None  # not applied yet
            await apply_inbox(s, run)
            await s.commit()
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            assert comment_received_at(run.payload) is not None
            assert "pr_comments" not in run.payload


async def test_the_same_comment_reaches_the_drive_once() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf)
        await _route(sf, ws, "only once")
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            assert len(await apply_inbox(s, run)) == 1
            await s.commit()
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            assert await apply_inbox(s, run) == []  # e.g. the next drive / next turn
            assert [c["body"] for c in run.payload["pr_comment_record"]] == ["only once"]


async def _post(sf, ws, run_id, kind: str, data: dict) -> None:  # noqa: ANN001
    async with sf() as s, workspace_session_scope(s, ws):
        run = await s.get(ExecutionRun, run_id)
        assert run is not None
        await post_to_inbox(s, run, kind, data)
        await s.commit()


async def test_pr_comments_and_issue_edits_are_applied_in_arrival_order() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf)
        edit = {"issue_number": 7, "intent_text": "edited intent", "ledger": []}
        await _post(sf, ws, run_id, ISSUE_EDIT, {**edit, "intent_text": "first edit"})
        await _route(sf, ws, "middle review")
        await _post(sf, ws, run_id, ISSUE_EDIT, {**edit, "intent_text": "last edit"})
        await _route(sf, ws, "final review")
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            messages = await apply_inbox(s, run)
            await s.commit()
        texts = [m["content"] for m in messages]
        assert len(texts) == 4
        for text, needle in zip(
            texts, ["first edit", "middle review", "last edit", "final review"], strict=True
        ):
            assert needle in text
