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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.identity.infrastructure.repositories import SqlAlchemyResourceBindingRepository
from backend.identity.workspaces_db import ProductRow, WorkspaceRow
from backend.workflow.application.intake.pr_comment import (
    consume_pr_comments,
    pr_comment_messages,
    route_pr_comment_to_run,
)
from backend.workflow.domain.pr_review import comment_received_at
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus
from backend.workflow.infrastructure.intake.db import RequestRow, TriggerEventRow, TriggerKind
from backend.workflow.infrastructure.workers.intake_worker import (
    IntakeWorker,
    IntakeWorkerConfig,
)
from plugin.github.webhook import parse_webhook
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

REPO = "acme/app"


def _payload(
    *,
    pr: bool = True,
    number: int = 1218,
    text: str = "fix the lint",
    association: str = "OWNER",
    login: str = "blasin",
) -> dict:
    issue: dict = {"number": number, "title": "t"}
    if pr:
        issue["pull_request"] = {"url": "x"}
    return {
        "github_event": "issue_comment",
        "repo": REPO,
        "body": {
            "issue": issue,
            "comment": {
                "body": text,
                "author_association": association,
                "user": {"login": login},
            },
        },
    }


async def _seed(
    sf: async_sessionmaker,  # noqa: ANN001
    *,
    run_status: RunStatus,
    watch_status: MergeWatchStatus = MergeWatchStatus.PENDING_CI,
    pr_number: int = 1218,
    workspace_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    run_id, ws = uuid.uuid4(), workspace_id or uuid.uuid4()
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
        assert run.payload["pr_comments"] == [
            {"pr_number": 1218, "body": "fix the lint", "author": "blasin"}
        ]


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


async def test_the_arrival_of_a_comment_outlives_the_consumed_queue() -> None:
    """``pr_comments`` is cleared when the drive starts; the record that a comment came is not."""
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(
            sf, run_status=RunStatus.FAILED, watch_status=MergeWatchStatus.FAILED
        )
        async with sf() as session:
            await route_pr_comment_to_run(session, workspace_id=ws, payload=_payload())
            await session.commit()
        run = await _run(sf, run_id, ws)
        consume_pr_comments(run)
        assert "pr_comments" not in run.payload
        assert comment_received_at(run.payload) is not None


async def test_drive_loop_reads_comment_once() -> None:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status=RunStatus.OPEN,
        payload={"pr_comments": [{"pr_number": 3, "body": "rename it", "author": "blasin"}]},
    )
    (message,) = pr_comment_messages(run)
    assert "rename it" in message["content"] and "#3" in message["content"]
    assert "blasin" in message["content"] and "founder" not in message["content"]
    consume_pr_comments(run)
    assert pr_comment_messages(run) == []


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


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIMER", ""])
async def test_stranger_comment_is_not_routed(association: str) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf, run_status=RunStatus.REVIEW_READY)
        async with sf() as session:
            payload = _payload(association=association, login="rando")
            assert await route_pr_comment_to_run(session, workspace_id=ws, payload=payload) is None
            await session.commit()
        run = await _run(sf, run_id, ws)
        assert run.status is RunStatus.REVIEW_READY
        assert "pr_comments" not in run.payload


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
async def test_trusted_comment_is_routed(association: str) -> None:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        run_id, ws = await _seed(sf, run_status=RunStatus.REVIEW_READY)
        async with sf() as session:
            payload = _payload(association=association)
            assert (
                await route_pr_comment_to_run(session, workspace_id=ws, payload=payload) == run_id
            )
            await session.commit()
        assert (await _run(sf, run_id, ws)).status is RunStatus.OPEN


async def _worker_scene(*, filters: dict, association: str = "COLLABORATOR", repo_permission=None):  # noqa: ANN001, ANN202
    """A watched PR run + a github comment trigger on a binding with ``filters``."""
    sf = async_sessionmaker(_ENGINE[0], expire_on_commit=False)
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
            trigger={"filters": filters},
        )
        await s.commit()
    run_id, _ = await _seed(sf, run_status=RunStatus.REVIEW_READY, workspace_id=ws)
    payload = {
        **_payload(association=association),
        "action": "created",
        "connector_account_id": str(account_id),
        "resource_id": REPO,
    }
    async with sf() as s, workspace_session_scope(s, ws):
        s.add(
            TriggerEventRow(
                id=uuid.uuid4(),
                workspace_id=ws,
                source="github",
                trigger_kind=TriggerKind.WEBHOOK,
                idempotency_key=f"k-{uuid.uuid4().hex}",
                payload=payload,
                received_at=datetime.now(UTC),
            )
        )
        await s.commit()
    await IntakeWorker(
        session_factory=sf, config=IntakeWorkerConfig(), repo_permission=repo_permission
    ).drain_once()
    async with sf() as s, workspace_session_scope(s, ws):
        requests = list((await s.execute(select(RequestRow))).scalars().all())
    return await _run(sf, run_id, ws), requests


_ENGINE: list = []


@pytest.fixture
async def _engine():  # noqa: ANN202
    async with db_engine(Base) as (engine, _pg):
        _ENGINE[:] = [engine]
        yield
        _ENGINE.clear()


async def test_paused_filter_binding_leaves_the_run_alone(_engine) -> None:  # noqa: ANN001
    run, requests = await _worker_scene(filters={"action": "never"})
    assert run.status is RunStatus.REVIEW_READY
    assert "pr_comments" not in run.payload
    assert requests == []


async def test_open_binding_routes_collaborator_comment_to_run(_engine) -> None:  # noqa: ANN001
    run, requests = await _worker_scene(filters={"action": "created"})
    assert run.status is RunStatus.OPEN
    assert [c["body"] for c in run.payload["pr_comments"]] == ["fix the lint"]
    assert requests == []


async def test_worker_does_not_route_a_stranger(_engine) -> None:  # noqa: ANN001
    run, _requests = await _worker_scene(filters={"action": "created"}, association="NONE")
    assert run.status is RunStatus.REVIEW_READY
    assert "pr_comments" not in run.payload
