"""Workflow application — intake trigger services.

Per v8 D29 (Intake absorption into Workflow context), the trigger-service
classes that adapt an external signal into a :class:`TriggerEvent` live
here. The Receive *stage* (filtering + binding resolution) lives one level
up at :mod:`backend.workflow.application.stages.intake`.

* :class:`DirectTrigger` — founder-typed text submission.
* :class:`WebhookReceiver` — connector-inbound delivery.
* :class:`DecisionResolutionTrigger` — re-dispatch on resolved decision.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
