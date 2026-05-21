"""Intake — Workflow §12.5 #8 (Bundle G).

Inbound trigger surface: every external signal (webhook / schedule /
direct / decision_resolution) is adapted into a :class:`TriggerEvent`
and persisted in :class:`TriggerEventRow` with an idempotency guard.
"""

from __future__ import annotations

from backend.intake.db import (
    IntakeBase,
    RequestRow,
    RequestStatus,
    TriggerEventRow,
    TriggerKind,
)
from backend.intake.decision_resolution import DecisionResolutionTrigger
from backend.intake.direct import DirectTrigger
from backend.intake.idempotency import is_duplicate, record
from backend.intake.schedule import ScheduleTrigger
from backend.intake.schema import TriggerEvent, TriggerKindLiteral
from backend.intake.webhook import WebhookReceiver

__all__ = [
    "DecisionResolutionTrigger",
    "DirectTrigger",
    "IntakeBase",
    "RequestRow",
    "RequestStatus",
    "ScheduleTrigger",
    "TriggerEvent",
    "TriggerEventRow",
    "TriggerKind",
    "TriggerKindLiteral",
    "WebhookReceiver",
    "is_duplicate",
    "record",
]
