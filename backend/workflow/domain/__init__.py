"""Workflow context — domain layer.

Hosts the per-run domain concepts (gate derivation · verification feedback ·
deliverable · execution target · client worktree …).

The lifecycle vocabulary is **not** here — it lives where it is persisted:
:mod:`backend.workflow.infrastructure.db` (``RunStatus`` / ``RunAttemptPhase``
/ ``WorkStepStatus`` / ``ProofState``) and
:mod:`backend.workflow.infrastructure.intake.db` (``RequestStatus``). Those
SQLAlchemy ``StrEnum`` declarations are the single source — a Postgres ENUM
is named after each one, so a second declaration would be a second source.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — public surface lives in nested modules.
__all__: list[str] = []
