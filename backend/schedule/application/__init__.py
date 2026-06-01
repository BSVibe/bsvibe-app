"""Schedule application — the emitter that turns a fire time into a
Workflow-side :class:`TriggerEvent`.

See :class:`~backend.schedule.application.emitter.ScheduleTrigger`.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
