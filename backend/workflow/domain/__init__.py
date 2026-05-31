"""Workflow context — domain layer.

Hosts the coarse v8 §7.2 state machine surface (``WorkflowState`` /
``WorkflowEvent`` / projection helpers / transition matrix) plus the
per-domain enums (``RequestStatus`` / ``WorkStepStatus`` / ``ProofState`` /
``RunAttemptPhase`` / ``DeliverableType`` / ``DeliverableStatus`` /
``ProofAspectType`` / ``ProofAspectStatus``) that the SQLAlchemy mirrors
in :mod:`backend.workflow.infrastructure.db` and :mod:`backend.workflow.infrastructure.intake.db` track.

Per v3 Q11 the v8 ``WorkflowState`` is a **projection** over the
per-domain enums — it does not replace them. The coarse enum is the
externally visible stage; the per-domain enums are the persistent
storage shape.
"""

from __future__ import annotations
