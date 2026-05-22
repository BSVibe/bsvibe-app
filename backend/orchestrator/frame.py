"""FrameStage — derive skill/artifact-type hints from a raw Request.

Workflow §12.5 #8 (Bundle G — Orchestrator). The frame stage is the 2nd
stage of the 3+ε state machine (Workflow §1). It inspects the trigger
payload + workspace's skill registry and decides which skill (if any)
should handle the request, plus a hint about the deliverable artifact_type.

Phase 1 implementation: substring keyword match against the description
field of every loaded SkillMeta. Bundle G's later integration may swap in
the retrieval-prime + classifier from Workflow §6 #5.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from backend.intake.db import RequestRow
from backend.orchestrator.schema import FramedRequest
from backend.skills.loader import SkillLoader

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class FrameConfig:
    """Caller-provided per-request frame context."""

    skill_loader: SkillLoader
    # Default artifact-type when the framer can't guess from skill content.
    default_artifact_type: str | None = None


class FrameStage:
    """Convert a raw Request into a framed plan."""

    async def frame(self, *, request: RequestRow, config: FrameConfig) -> FramedRequest:
        """Inspect the request and return framing hints."""
        text = _extract_text(request)
        skill_match = _match_skill(text, config.skill_loader)
        artifact_hint = _guess_artifact_type(skill_match, config.skill_loader) or (
            config.default_artifact_type
        )
        logger.info(
            "frame_stage_resolved",
            request_id=str(request.id),
            workspace_id=str(request.workspace_id),
            skill_match=skill_match,
            artifact_type_hint=artifact_hint,
        )
        return FramedRequest(skill_match=skill_match, artifact_type_hint=artifact_hint)


def _extract_text(request: RequestRow) -> str:
    """Pull a flat text representation out of a Request payload."""
    payload = request.payload or {}
    parts: list[str] = []
    if isinstance(payload, dict):
        for key in ("text", "title", "summary", "body", "message"):
            value = payload.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(parts).lower()


def _match_skill(text: str, loader: SkillLoader) -> str | None:
    """Pick the first skill whose name or description is referenced in the text."""
    if not text:
        return None
    for skill in loader.registry.values():
        haystack = f"{skill.name} {skill.description}".lower()
        # Crude but deterministic: any tokenized keyword from the skill
        # description appearing in the request text counts as a match.
        for word in haystack.split():
            if len(word) >= 4 and word in text:
                return skill.name
    return None


_ARTIFACT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("pull request", "pr"),
    ("page", "page"),
    ("image", "page_image"),
    ("code", "code"),
)


def _guess_artifact_type(skill_name: str | None, loader: SkillLoader) -> str | None:
    """If the skill's description hints at an artifact_type, surface it."""
    if skill_name is None:
        return None
    skill = loader.registry.get(skill_name)
    if skill is None:
        return None
    desc = skill.description.lower()
    for keyword, artifact in _ARTIFACT_KEYWORDS:
        if keyword in desc:
            return artifact
    return None


__all__ = ["FrameConfig", "FrameStage"]
