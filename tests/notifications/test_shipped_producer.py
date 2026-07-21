"""Producer-existence proof for the ``shipped`` outbox event (Notifier N3).

[P-shipped] drives the REAL verified-terminal write (``write_verified_deliverable``
— the single "shipped to the world" moment, NOT a mid-loop partial or a knowledge
answer) against a real DB and asserts a real ``NotificationEventRow(event="shipped",
dedupe_key="shipped:<deliverable_id>")`` lands in the SAME transaction as the
Deliverable. [D] a re-run of the write is deduped to one row by the UNIQUE key.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import backend.identity.workspaces_db  # noqa: F401 — register table on the shared Base
import backend.notifications.db  # noqa: F401 — register table on the shared Base
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import NotificationEventRow, NotificationStatus
from backend.workflow.domain.verified_deliverable import (
    write_answer_deliverable,
    write_verified_deliverable,
)
from backend.workflow.infrastructure.db import ExecutionRun, RunStatus

from .._support import memory_session

pytestmark = pytest.mark.asyncio


async def _seed_run(
    s, *, intent: str = "ship it", workspace_id: uuid.UUID | None = None
) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=workspace_id or uuid.uuid4(),
        product_id=None,
        request_id=uuid.uuid4(),
        status=RunStatus.RUNNING,
        payload={"intent_text": intent},
    )
    s.add(run)
    await s.flush()
    return run


async def _seed_workspace(s, *, language: str) -> uuid.UUID:
    ws = uuid.uuid4()
    s.add(WorkspaceRow(id=ws, name="WS", language=language))
    await s.flush()
    return ws


async def test_verified_deliverable_emits_shipped() -> None:
    """[P-shipped] the verified terminal queues a ``shipped`` notification."""
    async with memory_session() as s:
        run = await _seed_run(s)
        deliverable = await write_verified_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            artifact_refs=["src/foo.py"],
            summary="Add foo\n\nChanged files:\n- src/foo.py",
        )
        await s.commit()

        row = (
            await s.execute(
                select(NotificationEventRow).where(NotificationEventRow.event == "shipped")
            )
        ).scalar_one()
        assert row.dedupe_key == f"shipped:{deliverable.id}"
        assert row.workspace_id == run.workspace_id
        assert row.status is NotificationStatus.PENDING
        assert row.payload["deliverable_id"] == str(deliverable.id)
        assert row.payload["run_id"] == str(run.id)
        assert row.payload["link"] == f"/deliverables/{deliverable.id}"
        assert row.payload["title"]


async def test_shipped_title_localizes_to_workspace_language() -> None:
    """A KO workspace gets a KO ``shipped`` title; the deliverable's title line
    stays verbatim as the body. An EN workspace gets the English title."""
    async with memory_session() as s:
        ko_ws = await _seed_workspace(s, language="ko")
        ko_run = await _seed_run(s, workspace_id=ko_ws)
        await write_verified_deliverable(
            s,
            ko_run,
            attempt_id=uuid.uuid4(),
            artifact_refs=["src/foo.py"],
            summary="dedup 유틸 추가\n\nChanged files:\n- src/foo.py",
        )
        en_ws = await _seed_workspace(s, language="en")
        en_run = await _seed_run(s, workspace_id=en_ws)
        await write_verified_deliverable(
            s,
            en_run,
            attempt_id=uuid.uuid4(),
            artifact_refs=["src/foo.py"],
            summary="Add dedup util\n\nChanged files:\n- src/foo.py",
        )
        await s.commit()

        ko_row = (
            await s.execute(
                select(NotificationEventRow).where(NotificationEventRow.workspace_id == ko_ws)
            )
        ).scalar_one()
        assert ko_row.payload["title"] == "작업 완료"
        assert ko_row.payload["body"] == "dedup 유틸 추가"

        en_row = (
            await s.execute(
                select(NotificationEventRow).where(NotificationEventRow.workspace_id == en_ws)
            )
        ).scalar_one()
        assert en_row.payload["title"] == "Done"


async def test_shipped_body_is_compact_card_work_line_plus_verify_line() -> None:
    """The shipped body is a compact two-line card: the work-summary title line +
    the verify line, extracted from the already-localized deliverable summary. The
    full changed-files list stays in the report, NOT in the chat card."""
    async with memory_session() as s:
        ko_ws = await _seed_workspace(s, language="ko")
        run = await _seed_run(s, workspace_id=ko_ws)
        summary = (
            "「Toolkit」 새 문자열 유틸 함수 추가\n\n"
            "바뀐 파일 2개:\n- src/a.py\n- src/b.py\n\n"
            "검증: 2개 확인 통과."
        )
        await write_verified_deliverable(
            s, run, attempt_id=uuid.uuid4(), artifact_refs=["src/a.py"], summary=summary
        )
        await s.commit()

        row = (
            await s.execute(
                select(NotificationEventRow).where(NotificationEventRow.workspace_id == ko_ws)
            )
        ).scalar_one()
        body = row.payload["body"]
        assert "「Toolkit」 새 문자열 유틸 함수 추가" in body
        assert "검증:" in body
        # The compact card does NOT dump the changed-files list — that lives in the report.
        assert "바뀐 파일" not in body
        assert "- src/a.py" not in body
        assert body == "「Toolkit」 새 문자열 유틸 함수 추가\n검증: 2개 확인 통과."


async def test_answer_deliverable_does_not_emit_shipped() -> None:
    """A knowledge-only answer is NOT a verified ship → no ``shipped`` row."""
    async with memory_session() as s:
        run = await _seed_run(s, intent="what is X?")
        await write_answer_deliverable(
            s,
            run,
            attempt_id=uuid.uuid4(),
            answer="X is Y.",
            knowledge_refs=["note-1"],
        )
        await s.commit()

        rows = (
            (
                await s.execute(
                    select(NotificationEventRow).where(NotificationEventRow.event == "shipped")
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


async def test_re_writing_verified_is_deduped_to_one_shipped_row() -> None:
    """[D] two writes for the same deliverable id queue exactly one ``shipped`` row."""
    async with memory_session() as s:
        run = await _seed_run(s)
        deliverable = await write_verified_deliverable(
            s, run, attempt_id=uuid.uuid4(), artifact_refs=[], summary="done"
        )
        # A retried terminal write re-emits the SAME deliverable id's shipped moment.
        from backend.notifications.emit import emit_notification

        await emit_notification(
            s,
            workspace_id=run.workspace_id,
            event="shipped",
            dedupe_key=f"shipped:{deliverable.id}",
            payload={"title": "t", "body": "b"},
            producer_id="workflow:verified_deliverable",
        )
        await s.commit()

        rows = (
            (
                await s.execute(
                    select(NotificationEventRow).where(NotificationEventRow.event == "shipped")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
