"""#1111 — "shipped" is said when the run SHIPS; the approval card is its own event.

The ``shipped`` notification fired at VERIFY time — when the run is only
``review_ready`` and, with Safe Mode on, waiting for the founder's approval. It
was really the approval card (telegram / slack / discord render 승인 / 거절 on it)
titled "Done". A founder who rejected it had been told "Done" about work that was
never delivered.

형님 ruled (2026-10-05): split it.

* ``review_ready`` — the verified result is ready; carries the approve/reject
  buttons (``test_review_ready_producer``).
* ``shipped`` — the run actually shipped (merged, or shipped locally). Every such
  move goes through ``run_status.move_run_status`` (#1110), so that is where it is
  said. No buttons — there is nothing left to approve.

A workspace whose saved matrix predates ``review_ready`` has no switch for it; an
absent switch reads "off", which would silently stop the approval cards. The
founder's old ``shipped`` switch governed those cards, so it keeps governing them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import backend.identity.workspaces_db  # noqa: F401 — register table on the shared Base
import backend.notifications.db  # noqa: F401 — register table on the shared Base
from backend.notifications.db import DEFAULT_EVENTS, DEFAULT_MATRIX, NotificationEventRow
from backend.notifications.notify_builders import NotificationContent, _approval_keyboard
from backend.workflow.application.run_status import move_run_status
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)
from backend.workflow.infrastructure.workers.notify_worker import channels_for_event

from .._support import memory_session


async def _seed(s, *, status: RunStatus = RunStatus.REVIEW_READY) -> ExecutionRun:
    run = ExecutionRun(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status=status,
        payload={},
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    s.add(run)
    await s.flush()
    s.add(
        Deliverable(
            id=uuid.uuid4(),
            run_id=run.id,
            workspace_id=run.workspace_id,
            deliverable_type=DeliverableType.CODE,
            payload={"summary": "결제 재시도 추가\n\nChanged files:\n- pay.py"},
            created_at=datetime.now(tz=UTC),
        )
    )
    await s.flush()
    return run


async def _events(s, event: str) -> list[NotificationEventRow]:
    rows = await s.execute(select(NotificationEventRow).where(NotificationEventRow.event == event))
    return list(rows.scalars().all())


@pytest.mark.asyncio
async def test_a_run_that_ships_says_shipped() -> None:
    async with memory_session() as s:
        run = await _seed(s)

        assert await move_run_status(s, run, RunStatus.SHIPPED, reason="PR #9 merged")
        await s.commit()

        rows = await _events(s, "shipped")
        assert len(rows) == 1
        assert rows[0].dedupe_key == f"shipped:{run.id}"
        assert rows[0].payload["run_id"] == str(run.id)
        assert rows[0].payload["body"] == "결제 재시도 추가"


@pytest.mark.asyncio
@pytest.mark.parametrize("to_status", [RunStatus.OPEN, RunStatus.CANCELLED, RunStatus.FAILED])
async def test_no_other_move_says_shipped(to_status: RunStatus) -> None:
    async with memory_session() as s:
        run = await _seed(s)

        assert await move_run_status(s, run, to_status, reason="x")
        await s.commit()

        assert await _events(s, "shipped") == []


@pytest.mark.asyncio
async def test_a_refused_ship_says_nothing() -> None:
    """The transition table refuses leaving shipped; nothing moved, nothing said."""
    async with memory_session() as s:
        run = await _seed(s, status=RunStatus.CANCELLED)

        assert not await move_run_status(s, run, RunStatus.SHIPPED, reason="x")
        await s.commit()

        assert await _events(s, "shipped") == []


def test_the_approval_buttons_ride_the_review_card_not_shipped() -> None:
    did = str(uuid.uuid4())
    review = NotificationContent(event="review_ready", title="t", body="b", deliverable_id=did)
    shipped = NotificationContent(event="shipped", title="t", body="b", deliverable_id=did)
    assert _approval_keyboard(review) is not None
    assert _approval_keyboard(shipped) is None


def test_review_ready_is_a_notification_moment_on_by_default() -> None:
    assert "review_ready" in DEFAULT_EVENTS
    assert DEFAULT_MATRIX["review_ready"] is True


@pytest.mark.parametrize(("shipped_switch", "expected"), [(True, {"telegram"}), (False, set())])
def test_a_matrix_saved_before_the_split_keeps_its_shipped_choice(
    shipped_switch: bool, expected: set[str]
) -> None:
    """No ``review_ready`` key → the founder's ``shipped`` switch decides."""
    legacy = {"needs_you": True, "shipped": shipped_switch}
    assert channels_for_event(legacy, event="review_ready", bound={"telegram"}) == expected


def test_an_explicit_review_ready_switch_wins() -> None:
    """Control — once the founder sets it, their choice stands."""
    matrix = {"shipped": True, "review_ready": False}
    assert channels_for_event(matrix, event="review_ready", bound={"telegram"}) == set()


def test_the_settings_view_shows_what_the_gate_will_do() -> None:
    """The PWA draws a switch from the returned matrix; an absent key draws "off".
    The view must show the inherited value, or the founder sees a switch that is
    off while their approval cards keep arriving (or the reverse)."""
    from types import SimpleNamespace

    from backend.notifications.serialization import prefs_view_from_row

    for shipped_switch in (True, False):
        row = SimpleNamespace(
            matrix={"needs_you": True, "shipped": shipped_switch},
            quiet_hours_enabled=False,
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
        )
        view = prefs_view_from_row(row, ["in_app", "telegram"])  # type: ignore[arg-type]
        assert view.matrix["review_ready"] is shipped_switch
