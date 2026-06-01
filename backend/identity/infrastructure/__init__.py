"""Identity infrastructure layer (Lift I-Repo-Identity).

Concrete Repository implementations live under
:mod:`backend.identity.infrastructure.repositories`.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
