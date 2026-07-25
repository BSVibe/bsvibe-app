"""[P·hop] Notify link ④ END-TO-END — decision → outbox → worker → sender.

The audit found a MOCK-HIDDEN wiring blind spot on the founder-notification hop:

* ``tests/notifications/test_outbox_producer.py`` drives the REAL producer
  (``create_decision`` → ``NOTIFICATION_OUTBOX``) but stops at the outbox row.
* ``tests/notifications/test_notify_worker.py`` drives the REAL consumer
  (``NotifyWorker.drain_once`` → sender) but **pre-seeds** the
  ``NotificationEventRow`` its ``_seed`` helper inserts by hand.

Neither test drives the WHOLE hop. So a regression that disconnected the
producer from the consumer — ``create_decision`` no longer emitting, or the
worker no longer draining what ``create_decision`` writes — would keep both
suites green. This test closes that gap: it drives the ACTUAL producer and
asserts the ACTUAL consumer observes it, with NOTHING seeded at the seam (no
hand-built ``NotificationEventRow`` — ``create_decision`` MUST produce it).

    create_decision (real producer, needs_you outbox row)
      → NotifyWorker.drain_once (real consumer)
      → capturing NotifySender (the ONE allowed fake — the SINK)

How it would fail if the hop were disconnected: if ``create_decision`` stopped
emitting the outbox row, the worker would drain 0 and the sender would receive
nothing; if the worker stopped reading ``needs_you`` rows, the row would never
settle to ``sent``. Either regression turns this red.

Runs on real Postgres (seeds decisions/runs/connector rows — SQLite lies about
FK/enum types; CI is PG). Skips when no PG is configured so it never silently
"passes" on SQLite.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Register every table these rows touch on the shared Base.metadata.
import backend.connectors.db  # noqa: F401
import backend.identity.workspaces_db  # noqa: F401
import backend.notifications.db  # noqa: F401
import backend.workflow.infrastructure.db  # noqa: F401
from backend.connectors.db import ConnectorAccountRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.notifications.notify_builders import NotificationContent
from backend.workflow.application.run_persistence import create_decision
from backend.workflow.infrastructure.db import Decision, ExecutionRun, RunStatus
from backend.workflow.infrastructure.workers.notify_worker import NotifyWorker

from .._support import db_engine, use_real_pg

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not use_real_pg(),
        reason="producer→consumer notify hop seeds FK rows — requires a reachable Postgres "
        "(BSVIBE_DATABASE_URL); never silently falls back to SQLite",
    ),
]


class _CapturingSender:
    """The SINK — records what the worker asked to send. Everything upstream of
    it (decision → outbox → drain) is real; this fake stands only for the actual
    connector ``@p.outbound`` send so the test observes what the hop produced."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.contents: list[NotificationContent] = []

    async def send(
        self,
        *,
        connector: str,
        content: NotificationContent,
        delivery_config: dict[str, object],
        signing_secret_ciphertext: str,
    ) -> None:
        self.sent.append(connector)
        self.contents.append(content)


@pytest_asyncio.fixture
async def sf() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with db_engine() as (engine, is_pg):
        # The skip guard above guarantees PG; assert it so a mis-set env can
        # never let this integration test quietly run on SQLite.
        assert is_pg, "notify hop e2e must run on real Postgres"
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace_with_telegram(
    sf: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    """Seed an EN workspace with a bound telegram notify channel + a prefs matrix
    that routes ``needs_you`` to telegram — so the worker has a real push target.

    Deliberately does NOT seed any NotificationEventRow: the row under test is
    the one ``create_decision`` will produce.
    """
    from backend.notifications.db import NotificationPrefsRow

    ws = uuid.uuid4()
    async with sf() as s:
        s.add(WorkspaceRow(id=ws, name="Notify Hop WS", timezone="UTC", language="en"))
        s.add(
            ConnectorAccountRow(
                workspace_id=ws,
                connector="telegram",
                webhook_token=uuid.uuid4().hex,
                signing_secret_ciphertext="ciphertext",
                delivery_config={"chat_id": "42"},
                is_active=True,
            )
        )
        s.add(
            NotificationPrefsRow(
                workspace_id=ws,
                matrix={"needs_you": {"in_app": True, "telegram": True}},
            )
        )
        await s.commit()
    return ws


async def test_decision_drives_the_founder_notification_all_the_way_to_the_sender(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """The whole hop: a real Decision emits the outbox row, the real worker drains
    it, and the sender receives the notification DERIVED from the decision — with
    no NotificationEventRow seeded at the seam."""
    ws = await _seed_workspace_with_telegram(sf)

    # 1. REAL producer — a run stops on a Decision; create_decision stages the
    #    ``needs_you`` outbox row in the SAME transaction. Nothing is pre-seeded.
    async with sf() as s:
        run = ExecutionRun(workspace_id=ws, status=RunStatus.RUNNING, payload={})
        s.add(run)
        await s.flush()
        decision = await create_decision(
            s,
            run,
            None,
            kind="ask_user_question",
            payload={"question": "Postgres or SQLite?"},
            rationale="the run needs the founder to choose the datastore",
        )
        await s.commit()
        decision_id = decision.id
        run_id = run.id

    # 2. REAL consumer — the worker claims the pending row and fans it out to the
    #    bound push channel via the capturing sender.
    sender = _CapturingSender()
    worker = NotifyWorker(session_factory=sf, sender=sender, pwa_url="https://app.example")
    processed = await worker.drain_once()

    # 3. The sender observed the notification derived from the decision.
    assert processed == 1
    assert sender.sent == ["telegram"], "the decision's notification never reached the sender"
    content = sender.contents[0]
    assert content.event == "needs_you"  # right event kind
    assert content.title == "A run needs your decision"  # localized needs_you title
    assert "Postgres or SQLite?" in content.body  # the founder's question rode through
    # The deep-link CTA was derived from the row's /brief link (needs_you deep-links there).
    assert content.link is not None and "/brief" in content.link

    # 4. The outbox row — the ONE create_decision produced, keyed on the decision —
    #    transitioned to its terminal ``sent`` state (the hop completed).
    from backend.notifications.db import NotificationEventRow, NotificationStatus

    async with sf() as s:
        row = (
            await s.execute(
                select(NotificationEventRow).where(
                    NotificationEventRow.dedupe_key == f"needs_you:{decision_id}"
                )
            )
        ).scalar_one()
        assert row.workspace_id == ws
        assert row.payload["run_id"] == str(run_id)
        assert row.payload["decision_id"] == str(decision_id)
        assert row.status is NotificationStatus.SENT
        assert row.sent_at is not None

        # Exactly one outbox row exists for this workspace — proof the row the
        # worker delivered is the one the producer made, not a seeded double.
        all_rows = (
            (
                await s.execute(
                    select(NotificationEventRow).where(NotificationEventRow.workspace_id == ws)
                )
            )
            .scalars()
            .all()
        )
        assert len(all_rows) == 1

        # Sanity: the Decision the notification derived from is really persisted.
        assert await s.get(Decision, decision_id) is not None


async def test_no_decision_means_the_worker_drains_nothing(
    sf: async_sessionmaker[AsyncSession],
) -> None:
    """Negative control — with no Decision (so no producer emit), the real worker
    has nothing to drain and the sender is never called. Pins that the row the
    other test delivers genuinely came from ``create_decision``, not the fixture."""
    await _seed_workspace_with_telegram(sf)  # channel is ready, but no decision fired

    sender = _CapturingSender()
    worker = NotifyWorker(session_factory=sf, sender=sender)

    assert await worker.drain_once() == 0
    assert sender.sent == []
