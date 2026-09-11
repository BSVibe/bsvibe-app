"""The notify boundary must LOAD the paused Decision — 게이트 3 후속.

``_decision_keyboard`` can only render what ``NotificationContent`` carries, and
``_emit_needs_you`` already writes ``decision_id`` into the outbox payload. But
``NotifyWorker._content`` never read it, so the content arrived with no
``decision_id``, no actions and no options — and the keyboard, correctly, drew
nothing.

This is the hop that makes the feature real. Pinning it separately matters
because the keyboard tests pass on a hand-built ``NotificationContent``: they
prove the RENDERING and would stay green forever against a boundary that loads
nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.notifications.db import NotificationEventRow, NotificationStatus
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun
from backend.workflow.infrastructure.workers.notify_worker import NotifyWorker

from .._support import db_engine


class _NoopSender:
    """The worker under test never sends here — only its content boundary runs."""

    async def send(self, *_args, **_kwargs) -> None:  # pragma: no cover - unused
        return None


pytestmark = pytest.mark.asyncio


async def _seed(session, *, kind: str, payload: dict) -> tuple[uuid.UUID, uuid.UUID]:
    workspace_id = uuid.uuid4()
    run = ExecutionRun(id=uuid.uuid4(), workspace_id=workspace_id, status="open")
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=workspace_id,
        decision=kind,
        payload=payload,
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.flush()
    return workspace_id, decision.id


def _row(workspace_id: uuid.UUID, decision_id: uuid.UUID) -> NotificationEventRow:
    return NotificationEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        event="needs_you",
        dedupe_key=f"needs_you:{decision_id}",
        payload={
            "title": "답해주세요",
            "body": "검증이 실패했어요.",
            "link": "/brief",
            "decision_id": str(decision_id),
        },
        status=NotificationStatus.PENDING,
    )


async def test_an_action_decision_arrives_with_its_localized_actions() -> None:
    """``human_review_required`` — 19 of prod's 49."""
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            ws, did = await _seed(session, kind="human_review_required", payload={})
            await session.commit()
            worker = NotifyWorker(
                session_factory=sf, sender=_NoopSender(), pwa_url="https://app.bsvibe.dev"
            )
            content = await worker._localized_content(session, _row(ws, did))

    assert content.decision_id == str(did)
    keys = [a.key for a in content.decision_actions]
    assert keys == ["ship", "retry", "discard"]
    # Labels arrive ALREADY localized — the keyboard does no i18n of its own.
    assert all(a.label for a in content.decision_actions)


async def test_an_ask_user_question_arrives_with_its_options() -> None:
    """``ask_user_question`` — the free-form shape, 11 of 14 carry options."""
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            ws, did = await _seed(
                session,
                kind="ask_user_question",
                payload={"question": "어떻게 할까요?", "options": ["예", "아니오", "나중에"]},
            )
            await session.commit()
            worker = NotifyWorker(
                session_factory=sf, sender=_NoopSender(), pwa_url="https://app.bsvibe.dev"
            )
            content = await worker._localized_content(session, _row(ws, did))

    assert list(content.decision_options) == ["예", "아니오", "나중에"]


async def test_a_resolved_decision_carries_no_answers() -> None:
    """Negative control: a Decision already answered must not offer buttons.

    The outbox can deliver late (retry, quiet hours). Rendering a live keyboard
    over a settled Decision invites a tap that can only fail.
    """
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            ws, did = await _seed(session, kind="human_review_required", payload={})
            decision = await session.get(Decision, did)
            assert decision is not None
            decision.status = DecisionStatus.RESOLVED
            await session.commit()
            worker = NotifyWorker(
                session_factory=sf, sender=_NoopSender(), pwa_url="https://app.bsvibe.dev"
            )
            content = await worker._localized_content(session, _row(ws, did))

    assert content.decision_actions == ()
    assert content.decision_options == ()


async def test_a_missing_decision_degrades_to_a_plain_card() -> None:
    """Never fail a notification because its Decision vanished.

    The founder still needs to be told; the link still works.
    """
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            worker = NotifyWorker(
                session_factory=sf, sender=_NoopSender(), pwa_url="https://app.bsvibe.dev"
            )
            content = await worker._localized_content(session, _row(uuid.uuid4(), uuid.uuid4()))

    assert content.decision_actions == ()
    assert content.decision_options == ()
    assert content.cta_url  # the brief link survives


async def test_a_shipped_row_does_not_load_a_decision() -> None:
    """Negative control: only ``needs_you`` pays for the extra read."""
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            ws, did = await _seed(session, kind="human_review_required", payload={})
            await session.commit()
            row = _row(ws, did)
            row.event = "shipped"
            worker = NotifyWorker(
                session_factory=sf, sender=_NoopSender(), pwa_url="https://app.bsvibe.dev"
            )
            content = await worker._localized_content(session, row)

    assert content.decision_actions == ()
