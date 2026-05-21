"""``ProjectContext`` — the substrate the CoT decomposer reads.

Captures the founder intent (Request) + the surrounding signal the
model would want to weigh before deciding how to structure the work.
Today that's the parent Direction body and any BSage knowledge
fragments that look relevant to the intent. Future consumers
(Discoverer in PR-3) read the same substrate, so the shape is
deliberately neutral — not "decomposer input", just "project context".

All optional inputs are fail-soft: BSage errors, a missing parent
Direction, a None KnowledgeClient — none break the build. Worst case
you get back a context with only the Request intent, which is still
enough for the decomposer to fall back to a single step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.execution.composer.knowledge_client import KnowledgeClient, KnowledgeFragment

# TODO(bundle-x-integration): out-of-scope source dep -- backend.src.models
# from backend.src.models import Direction, Request

logger = structlog.get_logger(__name__)

_KNOWLEDGE_TOP_K = 5
_FRAGMENT_EXCERPT_CHARS = 280


@dataclass(frozen=True)
class ProjectContext:
    """Rendered for the decomposer (and future Discoverer) prompt.

    All fields are immutable on purpose — the same instance is read by
    multiple consumers (decomposer prompt + structured log lines).
    """

    request_intent: str
    direction_body: str | None = None
    knowledge_fragments: tuple[KnowledgeFragment, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """Format as a single user-message block. Empty sections are
        omitted so the LLM doesn't waste attention on "Direction: (none)".
        """
        sections: list[str] = [f"Request intent:\n{self.request_intent.strip()}"]
        if self.direction_body:
            sections.append(f"Direction:\n{self.direction_body.strip()}")
        if self.knowledge_fragments:
            lines = ["Knowledge fragments (prior project memory):"]
            for fragment in self.knowledge_fragments:
                excerpt = (fragment.excerpt or "").strip().replace("\n", " ")
                if len(excerpt) > _FRAGMENT_EXCERPT_CHARS:
                    excerpt = excerpt[: _FRAGMENT_EXCERPT_CHARS - 1] + "…"
                lines.append(f"- [{fragment.title}] {excerpt}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)


async def build_project_context(
    *,
    request: Request,
    session: AsyncSession,
    knowledge_client: KnowledgeClient | None = None,
) -> ProjectContext:
    """Resolve a ``ProjectContext`` for ``request``.

    - ``direction_body`` is fetched from ``request.origin_direction_id``
      when set; otherwise left None.
    - ``knowledge_fragments`` come from ``knowledge_client.search`` when
      a client is passed. ``NoopKnowledgeClient`` returns an empty list
      by design. Real-client failures (BSage down, transient HTTP
      error) are swallowed — the caller proceeds with a narrower
      context, never with an exception.
    """
    intent = (request.intent or "").strip()

    direction_body: str | None = None
    if request.origin_direction_id is not None:
        direction = await session.get(Direction, request.origin_direction_id)
        if direction is not None:
            direction_body = direction.body

    fragments: tuple[KnowledgeFragment, ...] = ()
    if knowledge_client is not None and intent:
        try:
            results = await knowledge_client.search(intent, top_k=_KNOWLEDGE_TOP_K)
            fragments = tuple(results)
        except Exception as exc:  # noqa: BLE001 — degrade context, never block dispatch
            logger.warning(
                "project_context_knowledge_search_failed",
                request_id=str(request.id),
                error=str(exc),
            )

    return ProjectContext(
        request_intent=intent,
        direction_body=direction_body,
        knowledge_fragments=fragments,
    )
