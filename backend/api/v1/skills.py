"""/api/v1/skills — list + get + create skills for the current workspace.

Skills are markdown manifests on disk under ``<skills_root>/<workspace_id>/``
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

import re
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import get_workspace_id
from backend.config import get_settings
from backend.skills import SkillLoader, SkillMeta

router = APIRouter()

# A created skill's filename + manifest name is this slug (the loader enforces
# the same grammar via SkillMeta — ^[a-z][a-z0-9-]*$).
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _slugify(name: str) -> str | None:
    """Derive a safe ``^[a-z][a-z0-9-]*$`` slug from a free-form name.

    Returns ``None`` when the name cannot yield a safe slug — including any name
    carrying a path separator or ``..`` (path-traversal defense: a created skill
    MUST stay inside the per-workspace dir, so we never derive a slug from a name
    that looks like a path).
    """
    if "/" in name or "\\" in name or ".." in name:
        return None
    # Lowercase; collapse any run of non-[a-z0-9] into a single hyphen.
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug or not _SLUG_RE.match(slug):
        return None
    return slug


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


def _skill_markdown(
    *, name: str, description: str, system_prompt: str, version: str = "1.0.0"
) -> str:
    """Render a skill ``.md`` matching the loader's on-disk format.

    Frontmatter fence (``---``…``---``) with the required fields, then the body.
    ``description`` is YAML-escaped via a JSON-style double-quoted scalar so a
    summary with colons / quotes round-trips through ``yaml.safe_load``. On
    create the default ``version`` is used; an update passes the skill's existing
    version so it is preserved.
    """
    import json  # noqa: PLC0415 — local: only this renderer needs it

    desc_scalar = json.dumps(description)  # valid YAML double-quoted scalar
    return (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {desc_scalar}\n"
        "---\n"
        f"{system_prompt.strip()}\n"
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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillCreate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> SkillResponse:
    """Create a skill: write ``<skills_root>/<workspace_id>/<slug>.md``.

    The name is slugified to a safe ``^[a-z][a-z0-9-]*$`` filename (422 when it
    can't yield one, incl. any path-traversal attempt). 409 when a skill with
    that slug already exists. The written manifest round-trips through
    ``SkillLoader`` — the 201 carries the parsed ``SkillResponse``.
    """
    slug = _slugify(body.name)
    if slug is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name must yield a slug matching ^[a-z][a-z0-9-]*$",
        )
    if not body.summary.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="summary must not be blank",
        )

    settings = get_settings()
    skill_dir = (Path(settings.skills_root) / str(workspace_id)).resolve()
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = (skill_dir / f"{slug}.md").resolve()

    # Path-safety: the resolved write target MUST stay inside the workspace dir.
    if md_path.parent != skill_dir:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid skill name",
        )
    if md_path.exists():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Skill '{slug}' already exists",
        )

    md_path.write_text(
        _skill_markdown(
            name=slug, description=body.summary.strip(), system_prompt=body.system_prompt
        ),
        encoding="utf-8",
    )

    loader = _loader_for(workspace_id)
    if slug not in loader.registry:  # pragma: no cover — written file must parse
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="created skill did not load",
        )
    return SkillResponse.from_meta(loader.get(slug))


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


@router.patch("/{name}")
async def update_skill(
    name: str,
    body: SkillUpdate,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> SkillResponse:
    """Update an existing skill's ``summary`` + ``system_prompt`` in place.

    Rewrites ``<skills_root>/<workspace_id>/<name>.md`` with the new description
    + body, preserving the existing ``name`` / ``version`` (the slug is immutable
    — no rename). 404 when no skill with that name is loaded; 422 when ``summary``
    is blank (it is the LLM invocation match signal). The rewritten manifest
    round-trips through ``SkillLoader`` — the 200 carries the parsed manifest.
    """
    loader = _loader_for(workspace_id)
    if name not in loader.registry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found"
        )
    if not body.summary.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="summary must not be blank",
        )

    existing = loader.get(name)
    settings = get_settings()
    skill_dir = (Path(settings.skills_root) / str(workspace_id)).resolve()
    md_path = (skill_dir / f"{name}.md").resolve()

    # Path-safety: the resolved write target MUST stay inside the workspace dir.
    # ``name`` is a registry key (a slug the loader accepted), but resolve-then-
    # check guards against any unexpected traversal in the route segment.
    if md_path.parent != skill_dir or not md_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Skill '{name}' not found"
        )

    md_path.write_text(
        _skill_markdown(
            name=existing.name,
            description=body.summary.strip(),
            system_prompt=body.system_prompt,
            version=existing.version,
        ),
        encoding="utf-8",
    )

    loader = _loader_for(workspace_id)
    if name not in loader.registry:  # pragma: no cover — rewritten file must parse
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="updated skill did not load",
        )
    return SkillResponse.from_meta(loader.get(name))
