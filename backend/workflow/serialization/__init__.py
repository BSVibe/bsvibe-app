"""Shared response shapes for the Workflow context.

The run / deliverable views the REST surface returns and the MCP tools
serialize are ONE definition, owned by the context that owns the rows — kept
apart from the SQLAlchemy modules so the presentation shape and the table
mapping stay separable (형님 판단 2026-08-21, the same call made for
``backend.schedule.serialization``).

Living here rather than under ``backend.api`` is what lets an MCP tool reuse
the shape: the import contract "MCP context depends only on Identity +
Workflow + Knowledge + common" forbids ``backend.mcp -> backend.api``, so a
view defined in the REST package can only be mirrored, never shared — and a
mirror is where the two surfaces start to disagree.
"""

from __future__ import annotations

__all__: list[str] = []
