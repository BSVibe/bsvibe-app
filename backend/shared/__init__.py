"""Common leaf modules shared across all bounded contexts.

Namespace-only. Holds cross-cutting infrastructure (``authz`` — JWT/RBAC,
``core`` — small pure helpers, ``fastapi`` — FastAPI-specific glue). Per
v8 §22 #2 + Lift N's import-linter contracts, ``backend.shared.*`` is a
leaf — it may NOT import from any bounded context.
"""

from __future__ import annotations

__all__: list[str] = []
