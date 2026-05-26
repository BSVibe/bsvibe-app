"""GDPR L1 — PII column catalog.

A frozen metadata declaration of which columns hold personal data, used as
a single referenceable source for future erasure / export refinements. No
behavior change today — but a column rename / table drop must surface here
loudly via :mod:`tests.data.test_pii_catalog`, not silently rot.

Scoping
-------
Only direct identifiers + the columns whose values are personal-data-shaped
appear here. JSON ``payload`` blobs that *may* incidentally contain PII
from user input are NOT exhaustively enumerated — those are addressed at
ingest / classification time, not by a schema-level catalog. Free-text
columns that may carry user-typed PII (``execution_decisions.rationale``,
``trigger_events.payload``) are flagged so a future erasure / export
refinement can fan out from this list.

The mapping is materialised through :class:`types.MappingProxyType` so
mutation attempts raise ``TypeError`` rather than silently corrupt the
catalog mid-process.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# Table → tuple of column names that hold (or may incidentally hold) PII.
# Stable, sorted, intentional minimal coverage for v1.
_RAW: dict[str, tuple[str, ...]] = {
    # Identity — direct identifiers.
    "users": ("email", "supabase_user_id"),
    "memberships": ("user_id", "invited_by_user_id"),
    # Workspace — name is founder-typed and may itself be personal-data-shaped.
    "workspaces": ("name",),
    # Execution decisions — rationale + payload may carry founder-typed PII.
    "execution_decisions": ("rationale", "actor_id", "resolved_by", "payload"),
    # Trigger events — inbound webhook payloads may carry sender PII.
    "trigger_events": ("payload",),
    # Requests — derived from trigger events; payload inherits the same risk.
    "requests": ("payload",),
    # Notification preferences — founder-set channel matrix is PII-shaped
    # (the per-channel destinations identify the founder).
    "notification_prefs": ("matrix",),
}

PII_CATALOG: Mapping[str, tuple[str, ...]] = MappingProxyType(_RAW)
"""Frozen ``{table: (column, …)}`` catalog of PII-bearing columns."""


__all__ = ["PII_CATALOG"]
