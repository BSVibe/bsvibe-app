"""The run pauses when the agent asks the founder — on EITHER transport (T1b).

On LiteLLM the agent asks by emitting an ``ask_user_question`` tool call, which the loop sees
in ``turn.tool_calls`` and acts on: Decision created, loop terminates ``needs_decision``.

An executor's CLI cannot emit that tool call — it asks by calling the MCP tool, which records
the Decision **out of band**, server-side. Nothing in ``turn.tool_calls`` says so. Without the
check under test here, the loop would carry on as if nothing had happened while the founder's
question sits pending, and the agent would keep working on a decision that was never made.

So the pause is owned by the SERVER, not by the agent's good behaviour: after each turn the
loop asks the run whether a question is pending. We already measured, today, that a coding CLI
trusts its own tools over anything the prompt tells it — "please stop" is a courtesy, not a
mechanism.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.application._drive_loop import _pending_question

pytestmark = pytest.mark.asyncio


class _Decision:
    def __init__(self, kind: str) -> None:
        self.id = uuid.uuid4()
        self.decision = kind
        self.payload = {"question": "Postgres or SQLite?"}


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> Any:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.queried = False

    async def execute(self, _stmt: Any) -> _Result:
        self.queried = True
        return _Result(self._rows)


class _Run:
    id = uuid.uuid4()
    workspace_id = uuid.uuid4()


async def test_an_unresolved_question_pauses_the_run() -> None:
    """The executor asked over MCP: the Decision exists, the tool call does not."""
    session = _Session([_Decision("ask_user_question")])

    pending = await _pending_question(session, _Run())

    assert pending is not None
    assert pending.payload["question"] == "Postgres or SQLite?"


async def test_no_question_does_not_pause() -> None:
    assert await _pending_question(_Session([]), _Run()) is None
