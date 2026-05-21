"""``ProjectContext`` — the substrate the CoT decomposer reads.

Captures the founder intent (Request) + the surrounding signal the
model would want to weigh before deciding how to structure the work.
Today that's the parent Direction body. Future consumers (Discoverer)
read the same substrate, so the shape is deliberately neutral — not
"decomposer input", just "project context".

All optional inputs are fail-soft: a missing parent Direction does not
break the build. Worst case you get back a context with only the
Request intent, which is still enough for the decomposer to fall back
to a single step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProjectContext:
    """Rendered for the decomposer (and future Discoverer) prompt.

    All fields are immutable on purpose — the same instance is read by
    multiple consumers (decomposer prompt + structured log lines).
    """

    request_intent: str
    direction_body: str | None = None

    def render(self) -> str:
        """Format as a single user-message block. Empty sections are
        omitted so the LLM doesn't waste attention on "Direction: (none)".
        """
        sections: list[str] = [f"Request intent:\n{self.request_intent.strip()}"]
        if self.direction_body:
            sections.append(f"Direction:\n{self.direction_body.strip()}")
        return "\n\n".join(sections)


async def build_project_context(
    *,
    request: Any,
    session: AsyncSession,
) -> ProjectContext:
    """Resolve a ``ProjectContext`` for ``request``.

    ``direction_body`` is fetched from ``request.origin_direction_id``
    when set; otherwise left None. Direction-fetch failures are
    swallowed — the caller proceeds with a narrower context.
    """
    intent = (getattr(request, "intent", "") or "").strip()

    direction_body: str | None = None
    direction_id = getattr(request, "origin_direction_id", None)
    if direction_id is not None:
        try:
            direction_mod = __import__("backend.execution.directions", fromlist=["Direction"])
            direction_cls = getattr(direction_mod, "Direction", None)
            if direction_cls is not None:
                direction = await session.get(direction_cls, direction_id)
                if direction is not None:
                    direction_body = getattr(direction, "body", None)
        except Exception as exc:  # noqa: BLE001 — degrade context, never block dispatch
            logger.warning(
                "project_context_direction_lookup_failed",
                request_id=str(getattr(request, "id", "")),
                error=str(exc),
            )

    return ProjectContext(
        request_intent=intent,
        direction_body=direction_body,
    )
