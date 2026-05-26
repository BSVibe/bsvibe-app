"""Receive stage (B10b) — between intake and Frame.

Workflow §0/§1/§3.1: every TriggerEvent should land via Receive BEFORE the
orchestrator's Frame sees it. Receive is responsible for:

* For connector-inbound triggers — resolve ``(connector_account_id,
  resource_id)`` to the matching :class:`ResourceBindingRow` (B10a) and
  populate routing hints (``product_id``, ``suggested_artifact_type``,
  selection echo) on the Request.
* Apply the binding's simple key-equality ``trigger.filters`` against the
  payload — non-matching → ``filtered_out`` (NO Request is created, but the
  trigger is honestly recorded via ``received_filtered`` in the row payload).
* For ``direct`` / ``schedule`` / ``decision_resolution`` — pass-through.

FK-safe seeding is mandatory: a binding lookup goes through real FKs to
``products`` + ``connector_accounts`` + ``workspaces``. Real Postgres enforces
them; SQLite does not — this has bitten prior PRs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.connectors.db import ConnectorAccountRow
from backend.intake.db import RequestRow, TriggerEventRow, TriggerKind
from backend.intake.receive import (
    RECEIVE_FILTERED_KEY,
    ReceiveOutcome,
    receive,
)
from backend.intake.schema import TriggerEvent
from backend.workers.intake_worker import IntakeWorker
from backend.workspaces.db import ProductRow, WorkspaceRow
from backend.workspaces.resource_bindings import ResourceBindingRepository

from .._support import db_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def sf() -> Any:
    async with db_engine() as (engine, _is_pg):
        yield async_sessionmaker(engine, expire_on_commit=False)


async def _seed_workspace_product_account(
    sf: async_sessionmaker[Any], *, workspace_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """FK-safe parent seeding — returns (product_id, connector_account_id)."""
    product_id = uuid.uuid4()
    account_id = uuid.uuid4()
    async with sf() as s:
        s.add(WorkspaceRow(id=workspace_id, name="ws", region="us-1", safe_mode=True))
        await s.flush()
        s.add(ProductRow(id=product_id, workspace_id=workspace_id, name="Blog", slug="blog"))
        s.add(
            ConnectorAccountRow(
                id=account_id,
                workspace_id=workspace_id,
                connector="github",
                webhook_token=f"tok-{uuid.uuid4().hex}",
                signing_secret_ciphertext="cipher",
            )
        )
        await s.commit()
    return product_id, account_id


def _make_trigger_row(
    *,
    workspace_id: uuid.UUID,
    source: str,
    trigger_kind: TriggerKind,
    payload: dict[str, Any],
) -> TriggerEventRow:
    return TriggerEventRow(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        source=source,
        trigger_kind=trigger_kind,
        idempotency_key=f"key-{uuid.uuid4().hex}",
        payload=payload,
        received_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# TriggerEvent schema additions — round-trip + backward compat
# ---------------------------------------------------------------------------


async def test_trigger_event_carries_new_optional_routing_fields() -> None:
    """Pydantic accepts the new spec fields (Workflow §3.1) — all optional."""
    workspace_id = uuid.uuid4()
    account_id = uuid.uuid4()
    correlation_id = uuid.uuid4()
    event = TriggerEvent(
        workspace_id=workspace_id,
        source="github",
        trigger_kind="webhook",
        idempotency_key="abc",
        payload={"action": "opened"},
        connector="github",
        connector_account_id=account_id,
        resource_id="bsvibe/bsvibe-site",
        suggested_artifact_type="code",
        suggested_skill="open_pr",
        intent_text="please review",
        actor="external",
        correlation_id=correlation_id,
    )
    assert event.connector == "github"
    assert event.connector_account_id == account_id
    assert event.resource_id == "bsvibe/bsvibe-site"
    assert event.suggested_artifact_type == "code"
    assert event.suggested_skill == "open_pr"
    assert event.intent_text == "please review"
    assert event.actor == "external"
    assert event.correlation_id == correlation_id
    # Round-trip via dump → re-parse.
    again = TriggerEvent.model_validate(event.model_dump())
    assert again == event


async def test_trigger_event_legacy_producer_still_works() -> None:
    """Producers that don't know about the new fields still construct cleanly."""
    event = TriggerEvent(
        workspace_id=uuid.uuid4(),
        source="github",
        trigger_kind="webhook",
        idempotency_key="abc",
        payload={"hello": "world"},
    )
    # Sensible defaults for every new field.
    assert event.connector is None
    assert event.connector_account_id is None
    assert event.resource_id is None
    assert event.suggested_artifact_type is None
    assert event.suggested_skill is None
    assert event.intent_text is None
    assert event.actor == "external"
    assert event.correlation_id is None


# ---------------------------------------------------------------------------
# Receive — connector-inbound resolution via B10a binding
# ---------------------------------------------------------------------------


async def test_receive_resolves_binding_and_populates_routing_hints(sf: Any) -> None:
    """Receive: (account, resource_id) → binding → product_id + selection echo."""
    workspace_id = uuid.uuid4()
    product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    async with sf() as s:
        repo = ResourceBindingRepository(s)
        await repo.create(
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="bsvibe/bsvibe-site",
            selection={"labels": ["bug"], "artifact_type": "code"},
            trigger={"enabled": True, "filters": {}},
        )
        await s.commit()

    row = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "bsvibe/bsvibe-site",
            "action": "opened",
        },
    )
    async with sf() as s:
        outcome = await receive(s, row)

    assert isinstance(outcome, ReceiveOutcome)
    assert outcome.filtered_out is False
    assert outcome.product_id == product_id
    assert outcome.suggested_artifact_type == "code"
    assert outcome.binding_id is not None
    # The request payload carries the selection echo for downstream Frame.
    assert outcome.request_payload["product_id"] == str(product_id)
    assert outcome.request_payload["suggested_artifact_type"] == "code"
    assert outcome.request_payload["selection"] == {"labels": ["bug"], "artifact_type": "code"}


async def test_receive_applies_filter_pass(sf: Any) -> None:
    """Filter ``{"action": "opened"}`` MATCHES → outcome passes."""
    workspace_id = uuid.uuid4()
    product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    async with sf() as s:
        repo = ResourceBindingRepository(s)
        await repo.create(
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="r",
            trigger={"enabled": True, "filters": {"action": "opened"}},
        )
        await s.commit()

    row = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "r",
            "action": "opened",
        },
    )
    async with sf() as s:
        outcome = await receive(s, row)
    assert outcome.filtered_out is False
    assert outcome.product_id == product_id


async def test_receive_applies_filter_reject(sf: Any) -> None:
    """Filter ``{"action": "opened"}`` REJECTS ``action=closed`` → filtered_out."""
    workspace_id = uuid.uuid4()
    product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    async with sf() as s:
        repo = ResourceBindingRepository(s)
        await repo.create(
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="r",
            trigger={"enabled": True, "filters": {"action": "opened"}},
        )
        await s.commit()

    row = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "r",
            "action": "closed",
        },
    )
    async with sf() as s:
        outcome = await receive(s, row)
    assert outcome.filtered_out is True
    assert outcome.reason is not None
    assert "filter" in outcome.reason


async def test_receive_no_binding_pass_through(sf: Any) -> None:
    """No binding for the (account, resource_id) → pass through (no product_id)."""
    workspace_id = uuid.uuid4()
    _product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    # NB: no binding seeded.
    row = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "unbound-repo",
            "action": "opened",
        },
    )
    async with sf() as s:
        outcome = await receive(s, row)
    assert outcome.filtered_out is False
    assert outcome.product_id is None
    assert outcome.binding_id is None


async def test_receive_direct_trigger_pass_through(sf: Any) -> None:
    """Direct triggers carry no connector_account_id — receive is a no-op pass."""
    workspace_id = uuid.uuid4()
    row = _make_trigger_row(
        workspace_id=workspace_id,
        source="direct",
        trigger_kind=TriggerKind.DIRECT,
        payload={"text": "hi there"},
    )
    async with sf() as s:
        outcome = await receive(s, row)
    assert outcome.filtered_out is False
    assert outcome.product_id is None
    assert outcome.binding_id is None
    # Pass-through means the payload is preserved on the Request.
    assert outcome.request_payload == {"text": "hi there"}


async def test_receive_schedule_trigger_pass_through(sf: Any) -> None:
    """Schedule triggers carry no resource_id — receive is a no-op pass."""
    workspace_id = uuid.uuid4()
    row = _make_trigger_row(
        workspace_id=workspace_id,
        source="schedule",
        trigger_kind=TriggerKind.SCHEDULE,
        payload={"cron_expr": "0 9 * * MON"},
    )
    async with sf() as s:
        outcome = await receive(s, row)
    assert outcome.filtered_out is False
    assert outcome.product_id is None


# ---------------------------------------------------------------------------
# Filter language — simple dict key-equality (multi-key AND)
# ---------------------------------------------------------------------------


async def test_receive_filter_multi_key_must_all_match(sf: Any) -> None:
    """Filters are dict AND — every key/value must match for a pass."""
    workspace_id = uuid.uuid4()
    product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    async with sf() as s:
        repo = ResourceBindingRepository(s)
        await repo.create(
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="r",
            trigger={
                "enabled": True,
                "filters": {"action": "opened", "github_event": "pull_request"},
            },
        )
        await s.commit()

    matching = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "r",
            "action": "opened",
            "github_event": "pull_request",
        },
    )
    async with sf() as s:
        assert (await receive(s, matching)).filtered_out is False

    partial = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "r",
            "action": "opened",  # match
            "github_event": "issues",  # no-match → reject
        },
    )
    async with sf() as s:
        assert (await receive(s, partial)).filtered_out is True


# ---------------------------------------------------------------------------
# IntakeWorker integration — drain_once honours Receive
# ---------------------------------------------------------------------------


async def test_intake_worker_creates_request_with_routing_hints(sf: Any) -> None:
    """End-to-end: TriggerEventRow → drain_once → RequestRow w/ product_id."""
    workspace_id = uuid.uuid4()
    product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    async with sf() as s:
        repo = ResourceBindingRepository(s)
        await repo.create(
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="repo-x",
            selection={"artifact_type": "page"},
            trigger={"enabled": True, "filters": {"action": "opened"}},
        )
        await s.commit()

    # Land an inbound TriggerEvent the worker will drain.
    async with sf() as s:
        s.add(
            _make_trigger_row(
                workspace_id=workspace_id,
                source="github",
                trigger_kind=TriggerKind.WEBHOOK,
                payload={
                    "connector_account_id": str(account_id),
                    "resource_id": "repo-x",
                    "action": "opened",
                    "github_event": "pull_request",
                },
            )
        )
        await s.commit()

    drained = await IntakeWorker(session_factory=sf).drain_once()
    assert drained == 1

    async with sf() as s:
        reqs = (await s.execute(select(RequestRow))).scalars().all()
    assert len(reqs) == 1
    req = reqs[0]
    assert req.payload.get("product_id") == str(product_id)
    assert req.payload.get("suggested_artifact_type") == "page"


async def test_intake_worker_skips_request_on_filter_reject(sf: Any) -> None:
    """Filter-rejected TriggerEvent → NO RequestRow is minted.

    But the TriggerEvent row is marked with a ``received_filtered`` record so
    the operator can see the trigger was not silently dropped.
    """
    workspace_id = uuid.uuid4()
    product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    async with sf() as s:
        repo = ResourceBindingRepository(s)
        await repo.create(
            workspace_id=workspace_id,
            product_id=product_id,
            connector_account_id=account_id,
            resource_id="repo-x",
            trigger={"enabled": True, "filters": {"action": "opened"}},
        )
        await s.commit()

    trig = _make_trigger_row(
        workspace_id=workspace_id,
        source="github",
        trigger_kind=TriggerKind.WEBHOOK,
        payload={
            "connector_account_id": str(account_id),
            "resource_id": "repo-x",
            "action": "closed",  # filter rejects
        },
    )
    async with sf() as s:
        s.add(trig)
        await s.commit()
        trig_id = trig.id

    drained = await IntakeWorker(session_factory=sf).drain_once()
    # drain_once returns ROWS PROCESSED (incl. filter rejects), not Requests created.
    assert drained == 1

    async with sf() as s:
        reqs = (await s.execute(select(RequestRow))).scalars().all()
        # NO Request was minted (the filter rejected).
        assert reqs == []

        # The TriggerEvent row carries an honest record of the filter rejection,
        # so the operator can see the trigger was not silently dropped.
        again = (
            await s.execute(select(TriggerEventRow).where(TriggerEventRow.id == trig_id))
        ).scalar_one()
        assert again.payload.get(RECEIVE_FILTERED_KEY) is not None

    # Idempotency: a subsequent drain doesn't reprocess the filtered trigger.
    again = await IntakeWorker(session_factory=sf).drain_once()
    assert again == 0


async def test_intake_worker_no_binding_falls_through(sf: Any) -> None:
    """No binding → today's behavior preserved: Request is minted, product_id None."""
    workspace_id = uuid.uuid4()
    _product_id, account_id = await _seed_workspace_product_account(sf, workspace_id=workspace_id)
    # No binding seeded.

    async with sf() as s:
        s.add(
            _make_trigger_row(
                workspace_id=workspace_id,
                source="github",
                trigger_kind=TriggerKind.WEBHOOK,
                payload={
                    "connector_account_id": str(account_id),
                    "resource_id": "unbound",
                    "action": "opened",
                },
            )
        )
        await s.commit()

    drained = await IntakeWorker(session_factory=sf).drain_once()
    assert drained == 1
    async with sf() as s:
        reqs = (await s.execute(select(RequestRow))).scalars().all()
    assert len(reqs) == 1
    assert reqs[0].payload.get("product_id") is None


async def test_intake_worker_direct_trigger_preserves_request(sf: Any) -> None:
    """Direct trigger (no connector context) → Request is minted unchanged."""
    workspace_id = uuid.uuid4()
    async with sf() as s:
        s.add(
            _make_trigger_row(
                workspace_id=workspace_id,
                source="direct",
                trigger_kind=TriggerKind.DIRECT,
                payload={"text": "hello"},
            )
        )
        await s.commit()

    drained = await IntakeWorker(session_factory=sf).drain_once()
    assert drained == 1
    async with sf() as s:
        reqs = (await s.execute(select(RequestRow))).scalars().all()
    assert len(reqs) == 1
    # The original payload survives (no routing hints injected).
    assert reqs[0].payload == {"text": "hello"}
