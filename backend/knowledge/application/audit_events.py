"""Audit events emitted by the ontology retraction / correction surface (M3a).

Mirrors the discipline established by
:mod:`backend.workflow.application.audit_events`: each high-signal event is
a tiny :class:`AuditEventBase` subclass with a pinned
``DEFAULT_EVENT_TYPE`` so the relay loop drains them through the SAME
outbox + delivery path. Payloads stay small — the rich payload sits on the
``ontology_corrections`` row.

Three events per design (§2.3) mirror the agent loop's start/progress/terminal
discipline:

* ``ontology.correction.requested`` — handler intake, undo-window opened.
* ``ontology.correction.undone`` — founder undid inside the window.
* ``ontology.correction.applied`` — worker / lazy-resolver wrote the vault
  tombstone (or applied the correction fields).

Workspace + actor + node attribution survive on every event so the future
"why did this node disappear" trace reads them as the source of truth.
Workspace-scoped retention (Lift Q1's ``audit_retention_days`` knob) applies
uniformly.
"""

from __future__ import annotations

from typing import ClassVar

from plugin.audit.events import AuditEventBase


class OntologyCorrectionRequested(AuditEventBase):
    """A founder issued a retraction or correction; undo window is open."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "ontology.correction.requested"


class OntologyCorrectionUndone(AuditEventBase):
    """A founder undid a retraction / correction inside the undo window."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "ontology.correction.undone"


class OntologyCorrectionApplied(AuditEventBase):
    """The undo window expired (or no undo arrived) — tombstone committed."""

    DEFAULT_EVENT_TYPE: ClassVar[str] = "ontology.correction.applied"


__all__ = [
    "OntologyCorrectionApplied",
    "OntologyCorrectionRequested",
    "OntologyCorrectionUndone",
]
