"""리뷰 코멘트 때문에 멈춘 PR 의 '다시 고치게 하기' 는 리뷰어의 말을 원래 런에 돌려준다.

``review_comment_unaddressed`` 는 ``merge_watch_stalled`` 의 한 갈래다. 같은 kind 의 retry 는
CI 실패를 되돌려 보내는 길(``failed_checks`` 로 ``ci_failure`` 를 만든다)이라, 여기서는 런이
"CI 가 실패했다" 는 엉뚱한 지시를 받고 리뷰어가 실제로 요청한 내용은 전달되지 않았다.
이미 shipped 인 런에 대한 보고(``shipped_with_waiting_deliverable``)는 되살릴 것이 없다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data.rls import workspace_session_scope
from backend.workflow.application._checkpoint_shared import (
    ACTION_ACKNOWLEDGE,
    _decision_actions,
)
from backend.workflow.application.checkpoint_resolution import resolve_checkpoint
from backend.workflow.infrastructure.db import (
    Decision,
    DecisionStatus,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.github.db import GithubMergeWatchRow, MergeWatchStatus

from ..._support import db_engine

pytestmark = pytest.mark.asyncio

_COMMENT = {"pr_number": 23, "body": "이 함수의 null 처리를 고쳐주세요", "author": "reviewer"}


@pytest_asyncio.fixture
async def sf():
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed(sf_, *, reason: str, run_status: RunStatus, payload_extra: dict | None = None):
    workspace_id, run_id, decision_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=UTC)
    async with sf_() as s, workspace_session_scope(s, workspace_id):
        s.add(
            ExecutionRun(
                id=run_id,
                workspace_id=workspace_id,
                status=run_status,
                payload={"intent_text": "리포트", **(payload_extra or {})},
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        s.add(
            Decision(
                id=decision_id,
                run_id=run_id,
                workspace_id=workspace_id,
                decision="merge_watch_stalled",
                payload={"reason": reason, "repo": "acme/x", "pr_number": 23},
                status=DecisionStatus.PENDING,
            )
        )
        s.add(
            GithubMergeWatchRow(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                run_id=run_id,
                deliverable_id=uuid.uuid4(),
                repo="acme/x",
                pr_number=23,
                branch="bsvibe/x",
                status=MergeWatchStatus.FAILED,
                next_poll_at=now + timedelta(days=1),
                deadline_at=now,
                last_error=reason,
            )
        )
        await s.commit()
    return workspace_id, run_id, decision_id


async def test_retry_puts_the_reviewers_comment_back_not_a_ci_failure(sf) -> None:
    from backend.workflow.application.intake.pr_comment import pr_comment_messages
    from backend.workflow.domain.pr_review import COMMENT_RECEIVED_AT_KEY

    workspace_id, run_id, decision_id = await _seed(
        sf,
        reason="review_comment_unaddressed",
        run_status=RunStatus.FAILED,
        # 소비되지 않는 기록: 코멘트가 도착한 때 + 본문 (``pr_comments`` 는 드라이브가 비웠다).
        payload_extra={
            COMMENT_RECEIVED_AT_KEY: datetime.now(tz=UTC).isoformat(),
            "pr_comment_record": [_COMMENT],
        },
    )

    async with sf() as s, workspace_session_scope(s, workspace_id):
        await resolve_checkpoint(
            s,
            workspace_id=workspace_id,
            checkpoint_id=decision_id,
            action_key="retry",
            actor_id=uuid.uuid4(),
        )
        await s.commit()

    async with sf() as s, workspace_session_scope(s, workspace_id):
        run = await s.get(ExecutionRun, run_id)
        watch = (await s.execute(select(GithubMergeWatchRow))).scalar_one()
    assert run is not None
    assert "ci_failure" not in run.payload
    assert run.status is RunStatus.OPEN
    assert run.payload["pr_comments"] == [_COMMENT]
    assert _COMMENT["body"] in pr_comment_messages(run)[0]["content"]
    assert watch.status is MergeWatchStatus.PENDING_CI


async def test_shipped_with_waiting_offers_only_acknowledge() -> None:
    decision = Decision(
        decision="merge_watch_stalled", payload={"reason": "shipped_with_waiting_deliverable"}
    )
    actions = _decision_actions(decision)
    assert actions is not None
    assert [a.key for a in actions] == [ACTION_ACKNOWLEDGE]


def test_a_consumed_queue_leaves_the_comment_in_the_record() -> None:
    """``consume_pr_comments`` 가 큐를 비워도 리뷰어의 말은 기록에 남고, 배송 뒤의 것만 걸러진다."""
    from backend.workflow.domain.pr_review import record_comment, unaddressed_comments

    payload: dict = {}
    first = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
    second = first + timedelta(hours=2)
    record_comment(payload, pr_number=23, body="첫 요청", author="r", received_at=first)
    record_comment(payload, pr_number=23, body="둘째 요청", author="r", received_at=second)

    delivered_between = first + timedelta(hours=1)
    assert [c["body"] for c in unaddressed_comments(payload, [delivered_between])] == ["둘째 요청"]
    assert len(unaddressed_comments(payload, [None])) == 2
