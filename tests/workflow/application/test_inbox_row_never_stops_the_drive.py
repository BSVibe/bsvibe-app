"""A malformed inbox row never stops the drive.

``apply_inbox`` runs at the start of every drive, between every turn and before every
verification. A row it cannot read — a PR comment whose ``pr_number`` is not a number, a
payload that is not an object, an edit whose ledger is not a list — used to raise out of it,
and with it out of the drive. Now such a row is skipped (and marked applied, so it is not
retried every turn), and the rows around it are applied as usual.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.data import Base
from backend.data.rls import workspace_session_scope
from backend.workflow.application.run_inbox import (
    APPLIED_KEY,
    INBOX_ACTIVITY,
    ISSUE_COMMENT,
    ISSUE_EDIT,
    PR_COMMENT,
    apply_inbox,
)
from backend.workflow.infrastructure.db import ExecutionRun, ExecutionRunActivity, RunStatus
from tests._support import db_engine

pytestmark = pytest.mark.asyncio

_LEDGER = [{"id": "R1", "text": "an item", "kind": "do"}]


async def _run_with(rows: list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    async with db_engine(Base) as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        ws, run_id = uuid.uuid4(), uuid.uuid4()
        async with sf() as s, workspace_session_scope(s, ws):
            s.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    status=RunStatus.RUNNING,
                    payload={"intent_text": "x", "requirements": _LEDGER},
                )
            )
            await s.flush()
            for data in rows:
                s.add(
                    ExecutionRunActivity(
                        run_id=run_id,
                        workspace_id=ws,
                        activity_type=INBOX_ACTIVITY,
                        payload=data,
                    )
                )
                await s.flush()
            await s.commit()
        async with sf() as s, workspace_session_scope(s, ws):
            run = await s.get(ExecutionRun, run_id)
            assert run is not None
            told = await apply_inbox(s, run)
            again = await apply_inbox(s, run)
            await s.commit()
            assert again == []  # nothing is retried, the bad rows included
            return told, dict(run.payload)


async def test_bad_rows_are_skipped_and_the_good_ones_still_told() -> None:
    told, payload = await _run_with(
        [
            {"kind": PR_COMMENT, "pr_number": "not-a-number", "body": "x", "author": "r"},
            ["not", "an", "object"],
            {"kind": ISSUE_EDIT, "issue_number": 7, "intent_text": "edited", "ledger": "oops"},
            {"kind": ISSUE_COMMENT, "issue_number": 7, "text": "a comment", "ledger": 5},
            {"kind": PR_COMMENT, "pr_number": 12, "body": "rename it", "author": "r"},
        ]
    )

    assert any("rename it" in m["content"] for m in told)
    assert len(payload[APPLIED_KEY]) == 5
    assert payload["requirements"] == _LEDGER  # a malformed ledger changes nothing


async def test_a_row_that_fails_halfway_leaves_no_half_applied_change(monkeypatch) -> None:  # noqa: ANN001
    import backend.workflow.application.run_inbox as inbox

    def _half(payload: dict[str, Any], data: Any) -> str:
        payload["intent_text"] = "half-applied"
        raise RuntimeError("fails after writing")

    monkeypatch.setitem(inbox._APPLY, "half", _half)
    told, payload = await _run_with([{"kind": "half"}])

    assert told == []
    assert payload["intent_text"] == "x"
