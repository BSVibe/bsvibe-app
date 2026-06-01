"""Workspace notification preferences — the events x channels enable matrix
plus a quiet-hours window. v1 stores the PREFERENCES only; actual email/Slack
delivery wiring is a later phase."""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
