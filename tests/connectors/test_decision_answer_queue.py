"""A chat tap QUEUES the answer; the engine applies it — 게이트 3 후속.

Answering a Decision is heavy work: ``resolve_checkpoint`` records the answer,
flips the run ``RUNNING → OPEN`` so a worker re-drives it, and for ``ship`` /
``discard`` runs side-effecting handlers. Doing that inside the inbound request
would put the engine's heaviest transition in a Telegram callback — which wants
a fast reply — and it would drag ``plugin.audit`` into the inbound layer, which
the R2c contract exists to prevent ("if they ever do, this gate surfaces it as
the reverse-direction violation it is").

The product ALREADY answers this the other way round for the heavier direction:
a chat MESSAGE does not create a run in the request. It lands a ``TriggerEvent``,
returns 202, and the IntakeWorker builds the run. The inbound layer records; the
engine decides.

So a decision tap follows the same rule. It validates (workspace scope, pending,
option in range), writes the founder's choice onto the Decision inside the
request transaction, and returns. The engine drains it.

Storing the queued answer on the Decision's own payload — rather than a new
outbox table — keeps it where the row it answers lives, makes the apply
idempotent (the key is cleared on success), and needs no migration. It mirrors
``audit_outbox``'s contract ("lands a row inside the request transaction, the
worker drains it on its own schedule") at the size this one call deserves.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.approval_callback import (
    ApprovalConnectorAdapter,
    handle_approval_callback,
)
from backend.connectors.db import ConnectorAccountRow
from backend.connectors.decision_answer_queue import (
    QUEUED_ANSWER_KEY,
    queued_answer,
)
from backend.identity.db import MembershipRow, UserRow
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun

from .._support import db_engine

pytestmark = pytest.mark.asyncio


class _FakeCipher:
    def decrypt(self, token: str) -> str:  # noqa: ARG002
        return "secret"


class _FakeRunner:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self._parsed = parsed
        self.calls: list[str] = []

    async def dispatch_action(
        self, plugin: Any, *, action_name: str, context: Any, kwargs: dict[str, Any]
    ) -> Any:
        del plugin, context, kwargs
        self.calls.append(action_name)
        return self._parsed if action_name == "parse" else {"ok": True}


def _adapter() -> ApprovalConnectorAdapter:
    return ApprovalConnectorAdapter(
        connector="fake",
        credential_key="fake_token",
        parse_action="parse",
        ack_action="ack",
        update_action="update",
        is_interaction=lambda body: body.get("kind") == "tap",
        is_authorized=lambda parsed, account: True,  # noqa: ARG005
        build_ack=lambda parsed, text: {"text": text},
        build_update=lambda parsed, status: {"status": status},
    )


def _account(ws: uuid.UUID) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        workspace_id=ws,
        connector="fake",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config={},
        is_active=True,
    )


async def _seed(session, ws: uuid.UUID, *, kind: str, payload: dict) -> uuid.UUID:
    owner = UserRow(id=uuid.uuid4(), supabase_user_id=f"sub-{uuid.uuid4().hex}")
    session.add(WorkspaceRow(id=ws, name="WS", language="en"))
    session.add(owner)
    session.add(MembershipRow(user_id=owner.id, workspace_id=ws, role="owner"))
    run = ExecutionRun(id=uuid.uuid4(), workspace_id=ws, status="running")
    session.add(run)
    await session.flush()
    decision = Decision(
        id=uuid.uuid4(),
        run_id=run.id,
        workspace_id=ws,
        decision=kind,
        payload=payload,
        status=DecisionStatus.PENDING,
    )
    session.add(decision)
    await session.commit()
    return decision.id


_RAW = json.dumps({"kind": "tap"}).encode()


def _parsed(verb: str, decision_id: str, answer: str) -> dict[str, Any]:
    return {
        "verb": verb,
        "deliverable_id": None,
        "decision_id": decision_id,
        "decision_answer": answer,
        "malformed": False,
    }


async def _tap(session, ws: uuid.UUID, parsed: dict[str, Any]) -> _FakeRunner:
    runner = _FakeRunner(parsed)
    handled = await handle_approval_callback(
        session=session,
        account=_account(ws),
        raw_body=_RAW,
        cipher=_FakeCipher(),
        adapter=_adapter(),
        plugin=object(),
        runner=runner,
    )
    assert handled is True
    return runner


async def test_an_action_tap_queues_and_does_not_resolve() -> None:
    """The inbound layer RECORDS. It must not flip the run itself."""
    ws = uuid.uuid4()
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            did = await _seed(session, ws, kind="human_review_required", payload={})
            await _tap(session, ws, _parsed("dca", str(did), "discard"))

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            # Still pending — the engine has not run yet.
            assert decision.status == DecisionStatus.PENDING
            queued = queued_answer(decision)
            assert queued is not None
            assert queued.action_key == "discard"
            assert queued.answer == ""
            assert queued.actor_id


async def test_an_option_tap_queues_the_TEXT_not_the_index() -> None:
    """The index is a transport detail of a 64-byte callback_data.

    Resolving it here — where the Decision is already loaded — means the engine
    never has to know a button existed, and a queued ``"1"`` can never reach the
    re-driven agent as the founder's answer.
    """
    ws = uuid.uuid4()
    options = ["이전 지시대로 대기한다", "실제 조사 질문을 새로 주신다"]
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            did = await _seed(session, ws, kind="ask_user_question", payload={"options": options})
            await _tap(session, ws, _parsed("dco", str(did), "1"))

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            queued = queued_answer(decision)
            assert queued is not None
            assert queued.answer == options[1]
            assert queued.action_key is None


async def test_an_out_of_range_option_queues_nothing() -> None:
    """A stale card taps an index the Decision no longer offers."""
    ws = uuid.uuid4()
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            did = await _seed(session, ws, kind="ask_user_question", payload={"options": ["예"]})
            await _tap(session, ws, _parsed("dco", str(did), "9"))

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            assert QUEUED_ANSWER_KEY not in (decision.payload or {})


async def test_a_second_tap_does_not_overwrite_a_queued_answer() -> None:
    """The card stays on the phone. Whichever button the founder pressed FIRST
    is the answer; a second tap must not race the engine's apply."""
    ws = uuid.uuid4()
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            did = await _seed(session, ws, kind="human_review_required", payload={})
            await _tap(session, ws, _parsed("dca", str(did), "discard"))
            runner = await _tap(session, ws, _parsed("dca", str(did), "ship"))

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            queued = queued_answer(decision)
            assert queued is not None
            assert queued.action_key == "discard"

    # Already-queued: ack only, no second card edit.
    assert runner.calls == ["parse", "ack"]


async def test_another_workspaces_decision_queues_nothing() -> None:
    """Workspace scope is the security boundary and it is checked BEFORE the write."""
    owner_ws, attacker_ws = uuid.uuid4(), uuid.uuid4()
    async with db_engine() as (engine, _pg):
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as session:
            did = await _seed(session, owner_ws, kind="human_review_required", payload={})
            await _seed(session, attacker_ws, kind="human_review_required", payload={})
            await _tap(session, attacker_ws, _parsed("dca", str(did), "discard"))

            session.expire_all()
            decision = await session.get(Decision, did)
            assert decision is not None
            assert QUEUED_ANSWER_KEY not in (decision.payload or {})


def test_the_inbound_layer_never_imports_the_resolver() -> None:
    """The contract this design exists to keep, asserted where it is easy to read.

    ``lint-imports`` enforces it across the whole graph; this states the ONE
    edge that made R2c fire, so a future edit that "simplifies" the queue back
    into a direct call fails here with the reason attached rather than as an
    opaque contract break.
    """
    import inspect

    from backend.connectors import approval_callback

    source = inspect.getsource(approval_callback)
    assert "checkpoint_resolution" not in source
    assert "resolve_checkpoint" not in source
