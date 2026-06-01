"""Router infrastructure layer — SQLAlchemy + external-service adapters.

Lift I-Repo-Router. Mirrors the Workflow context split (v8 D44/D45).
Concrete impls of the Protocols declared in
:mod:`backend.router.domain.repositories` live here.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
