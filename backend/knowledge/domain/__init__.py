"""Knowledge context — domain layer (Lift I-Repo-Knowledge).

Houses the Repository Protocols that the Knowledge context's application code
depends on. Concrete implementations live in
:mod:`backend.knowledge.infrastructure.repositories`.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
