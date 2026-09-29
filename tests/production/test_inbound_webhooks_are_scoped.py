"""[P] #959 — the public webhook paths publish the account's workspace.

A connector webhook carries no user session: the route authenticates it by the
``(connector, webhook_token)`` pair and gets a ``ConnectorAccountRow`` — which
names exactly one workspace. Nothing published it, so the full-suite probe
(2026-09-29, transaction-independent) caught two inbound paths touching
RLS-forced tables with an EMPTY GUC:

* ``handle_approval_callback`` — a phone tap on a ``needs_you`` card reads and
  writes the Decision. Telegram / Slack call it inside the request; Discord calls
  it from a background task on its own session, which is why the scope lives in
  the handler and not on the route.
* ``_resolve_inbound_product`` — a GitHub delivery is bound to the product whose
  ``repo_url`` matches, read from ``products``.

Under the fail-closed policy (#959 ③) both read nothing: the tap acks
"already handled" and changes nothing; the issue lands on no product.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.webhooks import _resolve_inbound_product
from backend.connectors.approval_callback import (
    ApprovalConnectorAdapter,
    handle_approval_callback,
)
from backend.connectors.db import ConnectorAccountRow
from backend.connectors.decision_answer_queue import queued_answer
from backend.data.rls import workspace_session_scope
from backend.identity.workspaces_db import ProductRow
from backend.workflow.infrastructure.db import Decision, DecisionStatus, ExecutionRun

from .conftest import PoliciedRead, PoliciedWrite, bootstrap_tenant, requires_real_pg

pytestmark = [pytest.mark.asyncio, requires_real_pg]


class _Cipher:
    def decrypt(self, token: str) -> str:  # noqa: ARG002
        return "secret"


class _Runner:
    def __init__(self, parsed: dict[str, Any]) -> None:
        self._parsed = parsed

    async def dispatch_action(
        self, plugin: Any, *, action_name: str, context: Any, kwargs: dict[str, Any]
    ) -> Any:
        del plugin, context, kwargs
        return self._parsed if action_name == "parse" else {"ok": True}


_ADAPTER = ApprovalConnectorAdapter(
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


def _account(ws: uuid.UUID, *, external_ref: str | None = None) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=ws,
        connector="fake",
        webhook_token=uuid.uuid4().hex,
        signing_secret_ciphertext="ciphertext",
        delivery_config={},
        external_ref=external_ref,
        is_active=True,
    )


async def _two_tenants(factory: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    return [
        await bootstrap_tenant(factory, supabase_user_id=f"in-{uuid.uuid4()}", email=e)
        for e in ("a@x.io", "b@x.io")
    ]


async def _pending_decision(factory: async_sessionmaker[AsyncSession], ws: uuid.UUID) -> uuid.UUID:
    async with factory() as session:
        async with workspace_session_scope(session, ws):
            run = ExecutionRun(id=uuid.uuid4(), workspace_id=ws, status="running")
            session.add(run)
            await session.flush()
            decision = Decision(
                id=uuid.uuid4(),
                run_id=run.id,
                workspace_id=ws,
                decision="human_review_required",
                payload={},
                status=DecisionStatus.PENDING,
            )
            session.add(decision)
            await session.flush()
        await session.commit()
        return decision.id


async def test_a_decision_tap_reads_and_writes_under_the_accounts_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
    policied_writes: list[PoliciedWrite],
) -> None:
    tenants = await _two_tenants(session_factory)
    decisions = {ws: await _pending_decision(session_factory, ws) for ws in tenants}
    policied_reads.clear()
    policied_writes.clear()

    async with session_factory() as session:
        for ws, did in decisions.items():
            parsed = {"verb": "dca", "decision_id": str(did), "decision_answer": "discard"}
            assert await handle_approval_callback(
                session=session,
                account=_account(ws),
                raw_body=json.dumps({"kind": "tap"}).encode(),
                cipher=_Cipher(),  # type: ignore[arg-type]
                adapter=_ADAPTER,
                plugin=object(),  # type: ignore[arg-type]
                runner=_Runner(parsed),  # type: ignore[arg-type]
            )
    reads, writes = list(policied_reads), list(policied_writes)

    # The tap landed for both tenants (fail-closed would ack "already" instead).
    async with session_factory() as session:
        for ws, did in decisions.items():
            async with workspace_session_scope(session, ws):
                decision = await session.get(Decision, did)
                assert decision is not None
                assert queued_answer(decision) is not None

    assert reads, "positive control: the tap read policied rows"
    assert [r for r in reads if r[1] == ""] == []
    assert {ws for t, ws, _ in writes if t == "execution_decisions"} == set(map(str, tenants))
    assert [w for w in writes if w[1] != w[2]] == []


async def test_a_github_delivery_finds_its_product_under_the_accounts_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    policied_reads: list[PoliciedRead],
) -> None:
    tenants = await _two_tenants(session_factory)
    products: dict[uuid.UUID, uuid.UUID] = {}
    for ws in tenants:
        pid = uuid.uuid4()
        async with session_factory() as session:
            async with workspace_session_scope(session, ws):
                session.add(
                    ProductRow(
                        id=pid,
                        workspace_id=ws,
                        name="p",
                        slug=f"p-{pid.hex[:12]}",
                        repo_url=f"https://github.com/acme/repo-{ws.hex[:6]}",
                    )
                )
                await session.flush()
            await session.commit()
        products[ws] = pid
    policied_reads.clear()

    async with session_factory() as session:
        for ws, pid in products.items():
            found = await _resolve_inbound_product(
                session,
                account=_account(ws),
                parsed_product_id=None,
                payload={"repo": f"acme/repo-{ws.hex[:6]}"},
            )
            assert found == pid

    assert any("products" in t for t, _ in policied_reads), policied_reads
    assert [r for r in policied_reads if r[1] == ""] == []
    # Each lookup ran under its own tenant — never another's, never '*'.
    assert {g for t, g in policied_reads if "products" in t} <= set(map(str, tenants))
