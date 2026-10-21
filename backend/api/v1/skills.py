"""/api/v1/skills — list + get + create skills for the current workspace.

Skills are markdown manifests in the workspace's skills store (files or rows, #929)
(per Workflow §6 #5 [locked]): a YAML frontmatter block (``name`` / ``version``
/ ``description`` + optional ``author`` / ``allowed_tools`` / ``model``) followed
by the Markdown system-prompt body. ``SkillLoader`` discovers + parses them.

``GET ""`` lists, ``GET /{name}`` fetches one, ``POST ""`` creates one by
writing a new ``.md`` that round-trips through the loader, and
``PATCH /{name}`` updates the editable body fields (``summary`` → manifest
``description`` and ``system_prompt`` → the Markdown body). The ``name`` /
slug is immutable on update (a rename would mean a file rename — deferred).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import get_workspace_id
from backend.extensions.skill import SkillMeta
from backend.extensions.skill.store import (
    SkillConflict,
    SkillInvalid,
    SkillNotFound,
    create_workspace_skill,
    update_workspace_skill,
    workspace_skill_loader,
)

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
    # The raw Markdown body — carried so an editor can round-trip it. Empty
    # string when the skill has no body (``has_system_prompt`` stays the flag).
    system_prompt: str = ""

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
            system_prompt=meta.system_prompt,
        )


class SkillCreate(BaseModel):
    """Create body for ``POST /api/v1/skills``.

    ``name`` is the human-friendly handle (slugified for the filename); ``summary``
    becomes the manifest ``description`` (the LLM invocation match signal);
    ``system_prompt`` is the Markdown body. CREATE only — no version/author/tools
    knobs in this lift; the written manifest carries a default version.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=100_000)


class SkillUpdate(BaseModel):
    """Update body for ``PATCH /api/v1/skills/{name}``.

    Only the editable body fields are mutable: ``summary`` (manifest
    ``description``) and ``system_prompt`` (Markdown body). The slug / ``name``
    is immutable — a rename would mean a file rename, deferred to a later lift —
    so it is NOT a field here (``extra=forbid`` rejects an attempt to send it).
    """

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=100_000)


@router.get("")
async def list_skills(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> list[SkillResponse]:
    """List all skills currently loaded for the workspace."""
    loader = await workspace_skill_loader(workspace_id)
    return [SkillResponse.from_meta(m) for m in loader.registry.values()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> SkillResponse:
    """Create a skill ``<slug>.md`` in the workspace's skills store.

    The name is slugified to a safe ``^[a-z][a-z0-9-]*$`` name (422 when it can't yield one,
    incl. any path-traversal attempt). 409 when a skill with that slug already exists — the
    create is atomic, so two concurrent creates cannot overwrite each other. The 201 carries
    the manifest as the loader parsed it.
    """
    try:
        meta = await create_workspace_skill(
            workspace_id, name=body.name, summary=body.summary, system_prompt=body.system_prompt
        )
    except SkillInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except SkillConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return SkillResponse.from_meta(meta)


@router.get("/{name}")
async def get_skill(
    name: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> SkillResponse:
    """Return one skill's manifest by name."""
    loader = await workspace_skill_loader(workspace_id)
    if name not in loader.registry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found"
        )
    return SkillResponse.from_meta(loader.get(name))


@router.patch("/{name}")
async def update_skill(
    name: str,
    body: SkillUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> SkillResponse:
    """Update an existing skill's ``summary`` + ``system_prompt`` in place.

    The ``name`` / ``version`` stay (the slug is immutable — no rename). 404 when no skill
    with that name exists; 422 when ``summary`` is blank (it is the LLM invocation match
    signal). The 200 carries the manifest as the loader parsed it.
    """
    try:
        meta = await update_workspace_skill(
            workspace_id, name=name, summary=body.summary, system_prompt=body.system_prompt
        )
    except SkillNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SkillInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return SkillResponse.from_meta(meta)
