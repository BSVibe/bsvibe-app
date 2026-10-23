"""A founder comment on a BSVibe-opened PR goes to the run that opened it.

Guards (founder ruling 2026-10-23): watched PR → no new run, original run OPEN with
the comment in its payload; unwatched PR → ``None`` (caller lands a new request);
BSVibe's own comment → the parser drops it.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application._loop_context import _consume_pr_comments, _pr_comment_messages
from backend.workflow.application.intake.pr_comment import route_pr_comment_to_run
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from plugin.github.webhook import parse_webhook
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

REPO = "acme/app"


def _payload(*, pr: bool = True, number: int = 1218, text: str = "fix the lint") -> dict:
    issue: dict = {"number": number, "title": "t"}
    if pr:
        issue["pull_request"] = {"url": "x"}
    return {
        "github_event": "issue_comment",
        "repo": REPO,
        "body": {"issue": issue, "comment": {"body": text}},
    }


async def _seed(
    sf: async_sessionmaker,  # noqa: ANN001
    *,
    run_status: RunStatus,
    watch_status: MergeWatchStatus = MergeWatchStatus.PENDING_CI,
    pr_number: int = 1218,
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id, ws = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(UTC)
    async with sf() as session:
        async with workspace_session_scope(session, ws):
            session.add(
                ExecutionRun(
                    id=run_id, workspace_id=ws, status=run_status, payload={"intent_text": "x"}
                )
            )
            session.add(
                GithubMergeWatchRow(
                    workspace_id=ws,
                    run_id=run_id,
                    deliverable_id=uuid.uuid4(),
                    repo=REPO,
                    pr_number=pr_number,
                    branch=f"bsvibe/run-{run_id.hex[:8]}",
                    status=watch_status,
                    next_poll_at=now,
                    deadline_at=now,
                )
            )
            await session.flush()
        await session.commit()
    return run_id, ws


async def _run(sf, run_id, ws) -> ExecutionRun:  # noqa: ANN001
    async with sf() as session, workspace_session_scope(session, ws):
        run = await session.get(ExecutionRun, run_id)
        assert run is not None
        return run


@pytest.mark.parametrize("status", [RunStatus.REVIEW_READY, RunStatus.FAILED])
async def test_comment_on_watched_pr_reopens_original_run(status: RunStatus) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf, run_status=status, watch_status=MergeWatchStatus.FAILED)
        async with sf() as session:
            routed = await route_pr_comment_to_run(session, workspace_id=ws, payload=_payload())
            await session.commit()
        assert routed == run_id
        run = await _run(sf, run_id, ws)
        assert run.status is RunStatus.OPEN
        assert run.payload["pr_comments"] == [{"pr_number": 1218, "body": "fix the lint"}]


async def test_comment_on_running_run_is_queued_without_second_run() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf, run_status=RunStatus.RUNNING)
        async with sf() as session:
            await route_pr_comment_to_run(session, workspace_id=ws, payload=_payload(text="a"))
            await route_pr_comment_to_run(session, workspace_id=ws, payload=_payload(text="b"))
            await session.commit()
        run = await _run(sf, run_id, ws)
        assert run.status is RunStatus.RUNNING
        assert [c["body"] for c in run.payload["pr_comments"]] == ["a", "b"]


async def test_unwatched_or_finished_pr_is_not_routed() -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf, run_status=RunStatus.REVIEW_READY)
        async with sf() as session:
            other_pr = _payload(number=7)
            not_a_pr = _payload(pr=False)
            assert await route_pr_comment_to_run(session, workspace_id=ws, payload=other_pr) is None
            assert await route_pr_comment_to_run(session, workspace_id=ws, payload=not_a_pr) is None
        merged_run, merged_ws = await _seed(
            sf,
            run_status=RunStatus.REVIEW_READY,
            watch_status=MergeWatchStatus.MERGED,
            pr_number=1300,
        )
        shipped_run, shipped_ws = await _seed(sf, run_status=RunStatus.SHIPPED, pr_number=1400)
        async with sf() as session:
            assert (
                await route_pr_comment_to_run(
                    session, workspace_id=merged_ws, payload=_payload(number=1300)
                )
                is None
            )
            assert (
                await route_pr_comment_to_run(
                    session, workspace_id=shipped_ws, payload=_payload(number=1400)
                )
                is None
            )
        assert (await _run(sf, run_id, ws)).status is RunStatus.REVIEW_READY
        assert "pr_comments" not in (await _run(sf, merged_run, merged_ws)).payload


async def test_drive_loop_reads_comment_once() -> None:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={"pr_comments": [{"pr_number": 3, "body": "rename it"}]},
    )
    (message,) = _pr_comment_messages(run)
    assert "rename it" in message["content"] and "#3" in message["content"]
    _consume_pr_comments(run)
    assert _pr_comment_messages(run) == []


def _delivery(text: str, *, sender: str = "User") -> bytes:
    return json.dumps(
        {
            "action": "created",
            "sender": {"type": sender},
            "repository": {"full_name": REPO},
            "issue": {"number": 1, "title": "t", "pull_request": {}},
            "comment": {"body": text},
        }
    ).encode()


def _parse(raw: bytes):  # noqa: ANN202
    return parse_webhook(
        workspace_id=uuid.uuid4(),
        headers={"X-GitHub-Event": "issue_comment", "X-GitHub-Delivery": "d1"},
        raw_body=raw,
    )


async def test_bsvibe_own_comment_is_ignored() -> None:
    assert _parse(_delivery("🤖 Opened a pull request to resolve this: u")) is None
    assert _parse(_delivery("x", sender="Bot")) is None
    assert _parse(_delivery("fix the lint")) is not None
