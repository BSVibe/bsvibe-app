"""NotifyWorker — drain the outbox, evaluate prefs/quiet-hours, deliver push (N2).

Covers the handoff §5 defect-class tests for the delivery half:

* [S] Soft-fail — one push channel raising never stops the others or wedges the
  queue; the row still settles to ``sent``.
* [Q] Quiet hours — inside the window push is suppressed and the worker does not
  error; the in-app inbox is unaffected (the worker never sends in_app anyway).
* [C-worker] Channel derivation — the worker attempts only channels that are
  BOTH matrix-enabled AND actually bound (available_channels ∩ enabled).

Plus the quiet-hours arithmetic (incl. the midnight wrap-around) and the
load-bearing ``FOR UPDATE SKIP LOCKED`` on the claim SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import time

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register the tables these tests touch on the shared Base.metadata.
import backend.connectors.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.db import (
    NotificationEventRow,
    NotificationPrefsRow,
    NotificationStatus,
)
from backend.notifications.notify_builders import NotificationContent
from backend.workflow.infrastructure.workers.notify_worker import (
    NotifyWorker,
    NotifyWorkerConfig,
    build_notify_claim_stmt,
    within_quiet_hours,
)

from .._support import db_engine

# asyncio_mode = "auto" runs the ``async def`` tests; the sync unit tests below
# (quiet-hours arithmetic, claim SQL) must NOT carry an explicit asyncio mark.


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


class _RecordingSender:
    """Test double for :class:`NotifySender`.

    Records the connectors it was asked to send on; raises for any connector in
    ``fail`` so a per-channel failure can be exercised.
    """

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.contents: list[NotificationContent] = []
        self._fail = fail or set()

    async def send(
        self,
        *,
        connector: str,
        content: NotificationContent,
        delivery_config: dict[str, object],
        signing_secret_ciphertext: str,
    ) -> None:
        if connector in self._fail:
            raise RuntimeError(f"{connector} boom")
        self.sent.append(connector)
        self.contents.append(content)


def _account(ws: uuid.UUID, connector: str, cfg: dict[str, object]) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        workspace_id=ws,
        connector=connector,
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config=cfg,
        is_active=True,
    )


async def _seed(
    sf: async_sessionmaker[AsyncSession],
    *,
    ws: uuid.UUID,
    connectors: list[tuple[str, dict[str, object]]],
    matrix: dict[str, dict[str, bool]] | None = None,
    quiet: tuple[str, str] | None = None,
    timezone: str = "UTC",
) -> uuid.UUID:
    """Seed a workspace + its connector bindings + prefs + a pending outbox row.

    Returns the pending :class:`NotificationEventRow` id.
    """
    async with sf() as s:
        s.add(WorkspaceRow(id=ws, name="Test WS", timezone=timezone))
        for connector, cfg in connectors:
            s.add(_account(ws, connector, cfg))
        if matrix is not None or quiet is not None:
            s.add(
                NotificationPrefsRow(
                    workspace_id=ws,
                    matrix=matrix if matrix is not None else {"needs_you": {"in_app": True}},
                    quiet_hours_enabled=quiet is not None,
                    quiet_hours_start=quiet[0] if quiet else "22:00",
                    quiet_hours_end=quiet[1] if quiet else "08:00",
                )
            )
        row = NotificationEventRow(
            workspace_id=ws,
            event="needs_you",
            dedupe_key=f"needs_you:{uuid.uuid4()}",
            payload={
                "title": "A run needs your decision",
                "body": "Postgres or SQLite?",
                "link": "/decisions",
            },
            status=NotificationStatus.PENDING,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _row(sf: async_sessionmaker[AsyncSession], row_id: uuid.UUID) -> NotificationEventRow:
    async with sf() as s:
        return (
            await s.execute(select(NotificationEventRow).where(NotificationEventRow.id == row_id))
        ).scalar_one()


async def test_soft_fail_one_channel_does_not_block_the_other(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """[S] two push channels, slack raises — telegram still sends, row → sent."""
    ws = uuid.uuid4()
    row_id = await _seed(
        sf,
        ws=ws,
        connectors=[("slack", {"channel": "C1"}), ("telegram", {"chat_id": "42"})],
        matrix={"needs_you": {"in_app": True, "slack": True, "telegram": True}},
    )
    sender = _RecordingSender(fail={"slack"})
    worker = NotifyWorker(session_factory=sf, sender=sender)

    processed = await worker.drain_once()

    assert processed == 1
    assert sender.sent == ["telegram"]  # slack raised; telegram still delivered
    row = await _row(sf, row_id)
    assert row.status is NotificationStatus.SENT  # queue not wedged
    assert row.sent_at is not None


async def test_all_channels_failing_leaves_row_pending_then_fails_after_budget(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """A fully-failed row retries up to the budget, then flips to failed."""
    ws = uuid.uuid4()
    row_id = await _seed(
        sf,
        ws=ws,
        connectors=[("telegram", {"chat_id": "42"})],
        matrix={"needs_you": {"in_app": True, "telegram": True}},
    )
    sender = _RecordingSender(fail={"telegram"})
    worker = NotifyWorker(
        session_factory=sf, sender=sender, config=NotifyWorkerConfig(max_attempts=2)
    )

    await worker.drain_once()
    assert (await _row(sf, row_id)).status is NotificationStatus.PENDING  # attempt 1, retry
    await worker.drain_once()
    assert (await _row(sf, row_id)).status is NotificationStatus.FAILED  # attempt 2 == budget


async def test_quiet_hours_suppresses_push_but_settles_the_row(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """[Q] inside quiet hours no push is sent and the worker does not error; the
    in-app inbox path is untouched (the worker never sends in_app), and the row
    still settles to sent."""
    ws = uuid.uuid4()
    # A 00:00–23:59 window in UTC guarantees "now" is inside quiet hours.
    row_id = await _seed(
        sf,
        ws=ws,
        connectors=[("telegram", {"chat_id": "42"})],
        matrix={"needs_you": {"in_app": True, "telegram": True}},
        quiet=("00:00", "23:59"),
        timezone="UTC",
    )
    sender = _RecordingSender()
    worker = NotifyWorker(session_factory=sf, sender=sender)

    processed = await worker.drain_once()

    assert processed == 1
    assert sender.sent == []  # push suppressed
    assert (await _row(sf, row_id)).status is NotificationStatus.SENT


async def test_channel_derivation_only_bound_and_enabled_channels(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """[C-worker] only channels BOTH matrix-enabled AND bound are attempted.

    slack is matrix-enabled but NOT bound (no connector) → not attempted; email
    is bound but matrix-disabled → not attempted; only telegram (enabled + bound)
    is sent.
    """
    ws = uuid.uuid4()
    await _seed(
        sf,
        ws=ws,
        connectors=[("telegram", {"chat_id": "42"}), ("email-sender", {"to": "a@b.c"})],
        matrix={
            "needs_you": {"in_app": True, "slack": True, "telegram": True, "email-sender": False}
        },
    )
    sender = _RecordingSender()
    worker = NotifyWorker(session_factory=sf, sender=sender)

    await worker.drain_once()

    assert sender.sent == ["telegram"]


async def test_no_push_channels_still_settles_sent(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """An in-app-only workspace (no push channels) is a completed, non-error
    outcome — the Decision surfaces in the inbox, nothing to send."""
    ws = uuid.uuid4()
    row_id = await _seed(
        sf,
        ws=ws,
        connectors=[],
        matrix={"needs_you": {"in_app": True}},
    )
    sender = _RecordingSender()
    worker = NotifyWorker(session_factory=sf, sender=sender)

    processed = await worker.drain_once()

    assert processed == 1
    assert sender.sent == []
    assert (await _row(sf, row_id)).status is NotificationStatus.SENT


async def test_drain_is_a_noop_when_the_outbox_is_empty(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    async with sf() as s:  # materialise the schema without seeding a row
        s.add(WorkspaceRow(id=uuid.uuid4(), name="empty"))
        await s.commit()
    worker = NotifyWorker(session_factory=sf, sender=_RecordingSender())
    assert await worker.drain_once() == 0


# ── quiet-hours arithmetic ────────────────────────────────────────────────────


def test_within_quiet_hours_simple_interval() -> None:
    assert within_quiet_hours(time(13, 0), "09:00", "17:00")
    assert not within_quiet_hours(time(8, 0), "09:00", "17:00")
    assert not within_quiet_hours(time(17, 0), "09:00", "17:00")  # end is exclusive


def test_within_quiet_hours_wraps_over_midnight() -> None:
    # 22:00–08:00 spans midnight.
    assert within_quiet_hours(time(23, 0), "22:00", "08:00")
    assert within_quiet_hours(time(2, 0), "22:00", "08:00")
    assert not within_quiet_hours(time(12, 0), "22:00", "08:00")


def test_within_quiet_hours_malformed_or_empty_window_is_disabled() -> None:
    assert not within_quiet_hours(time(12, 0), "bad", "08:00")
    assert not within_quiet_hours(time(12, 0), "08:00", "08:00")  # zero-width window


# ── claim SQL carries the multi-server lock hint ──────────────────────────────


def test_claim_stmt_uses_for_update_skip_locked() -> None:
    from sqlalchemy.dialects import postgresql

    sql = str(build_notify_claim_stmt(batch_size=10).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
