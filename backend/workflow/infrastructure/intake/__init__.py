"""Workflow infrastructure — intake persistence.

Per v8 D29, the intake-side SQLAlchemy tables (``trigger_events`` +
``requests``) live here. See :mod:`backend.workflow.infrastructure.intake.db`.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
