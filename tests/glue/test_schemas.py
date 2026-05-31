"""Pydantic schema invariants per Workflow §3.1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.delivery.schema import ActionResult, DeliveryResult
from backend.intake.schema import TriggerEvent


def test_trigger_event_minimal() -> None:
    ev = TriggerEvent(
        workspace_id=uuid.uuid4(),
        source="github",
        trigger_kind="webhook",
        idempotency_key="abc-123",
        payload={"action": "opened"},
        received_at=datetime.now(tz=UTC),
    )
    assert ev.source == "github"


def test_trigger_event_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        TriggerEvent(
            workspace_id=uuid.uuid4(),
            source="x",
            trigger_kind="direct",
            idempotency_key="k",
            payload={},
            received_at=datetime.now(tz=UTC),
            extra_unknown_field="boom",  # type: ignore[call-arg]
        )


def test_delivery_result_with_actions() -> None:
    dr = DeliveryResult(
        workspace_id=uuid.uuid4(),
        deliverable_id=uuid.uuid4(),
        artifact_type="pr",
        actions=[
            ActionResult(action="open_pr", succeeded=True, output={"url": "https://x"}),
            ActionResult(action="notify", succeeded=False, error="rate-limited"),
        ],
        delivered_at=datetime.now(tz=UTC),
    )
    assert len(dr.actions) == 2
    assert dr.actions[1].succeeded is False
