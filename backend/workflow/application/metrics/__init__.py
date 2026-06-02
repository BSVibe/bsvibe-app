"""Proof-surface metrics package (Lift M4a).

Pure aggregation services over existing audit_outbox + settle_drains +
execution_runs rows. No new schema, no new audit events. Per the design
SoT ``BSVibe_Proof_Surface_Design_2026-05-30.md`` §6, every signal here
is computed from data already on disk.

See :mod:`.trust_surface` for the per-product trust metric service
backing the L0 Fleet glance arrow and the L3 Inside trust panel.
"""

from __future__ import annotations

__all__: list[str] = []
