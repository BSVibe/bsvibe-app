"""Knowledge context — domain layer.

Holds :mod:`.retraction`. The Repository Protocols that used to live here
(note / proposal / canonical-anchor) were deleted 2026-08-20 along with the
producer-less Postgres mirror they read: nothing wrote those tables, and the
vault is the source of truth for knowledge.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
