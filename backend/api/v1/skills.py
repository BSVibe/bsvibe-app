"""/api/v1/skills — list + get loaded skills for the current workspace.

The MD-file write surface (CREATE/UPDATE/DELETE) intentionally does NOT exist
on the HTTP API in Phase 1: per Workflow §6 #5 [locked], skill authoring is
file-system based (lives in the per-workspace ``<skills_root>/<workspace_id>/``
dir). Curl + git commit, not POST.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from backend.api.deps import get_workspace_id
from backend.config import get_settings
from backend.skills import SkillLoader, SkillMeta

router = APIRouter()


class SkillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    description: str
    author: str = ""
    allowed_tools: list[str] = []
    model: str | None = None
    has_system_prompt: bool = False

    @classmethod
    def from_meta(cls, meta: SkillMeta) -> SkillResponse:
        return cls(
            name=meta.name,
            version=meta.version,
            description=meta.description,
            author=meta.author,
            allowed_tools=list(meta.allowed_tools),
            model=meta.model,
            has_system_prompt=bool(meta.system_prompt),
        )


def _loader_for(workspace_id: uuid.UUID) -> SkillLoader:
    settings = get_settings()
    skill_dir = Path(settings.skills_root) / str(workspace_id)
    loader = SkillLoader(skill_dir)
    loader.load_all()
    return loader


@router.get("")
async def list_skills(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> list[SkillResponse]:
    """List all skills currently loaded for the workspace."""
    loader = _loader_for(workspace_id)
    return [SkillResponse.from_meta(m) for m in loader.registry.values()]


@router.get("/{name}")
async def get_skill(
    name: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> SkillResponse:
    """Return one skill's manifest by name."""
    loader = _loader_for(workspace_id)
    if name not in loader.registry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found"
        )
    return SkillResponse.from_meta(loader.get(name))
