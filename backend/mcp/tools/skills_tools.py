"""Skills tools — UI-parity surface (Lift D3c).

Wraps the per-workspace skills registry the PWA's
:mod:`apps/pwa/components/skills/{SkillsLibrary,SkillViewer,SkillEditor}`
view drives via ``/api/v1/skills``. Handlers re-use
:class:`SkillLoader` (the same loader the REST endpoints call) so the
MCP and PWA paths see the same on-disk registry under
``<skills_root>/<workspace_id>/``.

PWA exposes list / get / create / update — there is no "run" button, so
this lift ships only those four. ``invoke_skill`` is the agent loop's
runtime entry point, not a founder-visible surface.

Scopes follow the existing convention: ``mcp:read`` for list / get,
``mcp:write`` for create / update.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.config import get_settings
from backend.extensions.skill import SkillLoader, SkillMeta
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


# Slug grammar — mirrors backend.api.v1.skills (the loader enforces the
# same shape via SkillMeta). Kept local so the MCP module doesn't reach
# into the forbidden backend.api subtree.
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _slugify(name: str) -> str | None:
    """Derive a safe ``^[a-z][a-z0-9-]*$`` slug from a free-form name.

    Returns ``None`` when the name cannot yield a safe slug — including
    any name carrying a path separator or ``..`` (path-traversal defense).
    Mirrors :func:`backend.api.v1.skills._slugify` 1:1.
    """
    if "/" in name or "\\" in name or ".." in name:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not slug or not _SLUG_RE.match(slug):
        return None
    return slug


def _skill_markdown(
    *, name: str, description: str, system_prompt: str, version: str = "1.0.0"
) -> str:
    """Render a skill ``.md`` matching the loader's on-disk format.

    Mirrors :func:`backend.api.v1.skills._skill_markdown` 1:1 so a created
    or updated skill round-trips through the loader identically to the
    REST surface.
    """
    desc_scalar = json.dumps(description)
    return (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: {desc_scalar}\n"
        "---\n"
        f"{system_prompt.strip()}\n"
    )


def _loader_for(workspace_id: uuid.UUID) -> SkillLoader:
    """Per-workspace :class:`SkillLoader`.

    Tests inject a pre-built loader via ``ctx.extras["skill_loader"]`` so a
    unit run never touches the on-disk skills dir. In production the loader
    reads ``<skills_root>/<workspace_id>/*.md``.
    """
    settings = get_settings()
    skill_dir = Path(settings.skills_root) / str(workspace_id)
    loader = SkillLoader(skill_dir)
    loader.load_all()
    return loader


def _loader_for_ctx(ctx: ToolContext) -> SkillLoader:
    cached = ctx.extras.get("skill_loader") if ctx.extras else None
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    return _loader_for(ctx.principal.workspace_id)


def _meta_to_dict(meta: SkillMeta) -> dict[str, Any]:
    return {
        "name": meta.name,
        "version": meta.version,
        "description": meta.description,
        "author": meta.author,
        "allowed_tools": list(meta.allowed_tools),
        "model": meta.model,
        "has_system_prompt": bool(meta.system_prompt),
        "system_prompt": meta.system_prompt,
    }


def _skill_dir(workspace_id: uuid.UUID) -> Path:
    settings = get_settings()
    return (Path(settings.skills_root) / str(workspace_id)).resolve()


# ---------------------------------------------------------------------------
# bsvibe_skills_list
# ---------------------------------------------------------------------------
class SkillsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(_args: SkillsListInput, ctx: ToolContext) -> Any:
    loader = _loader_for_ctx(ctx)
    return _Envelope([_meta_to_dict(m) for m in loader.registry.values()])


# ---------------------------------------------------------------------------
# bsvibe_skills_get
# ---------------------------------------------------------------------------
class SkillsGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


async def _h_get(args: SkillsGetInput, ctx: ToolContext) -> Any:
    loader = _loader_for_ctx(ctx)
    if args.name not in loader.registry:
        raise ToolError(f"skill not found: {args.name}")
    return _Envelope(_meta_to_dict(loader.get(args.name)))


# ---------------------------------------------------------------------------
# bsvibe_skills_create
# ---------------------------------------------------------------------------
class SkillsCreateInput(BaseModel):
    """Mirror of :class:`SkillCreate` (REST).

    ``name`` is slugified to a safe ``^[a-z][a-z0-9-]*$`` filename;
    ``summary`` becomes the manifest ``description``; ``system_prompt`` is
    the Markdown body. CREATE only — no version/author/tools knobs in this
    lift; the written manifest carries a default version.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=100_000)


async def _h_create(args: SkillsCreateInput, ctx: ToolContext) -> Any:
    slug = _slugify(args.name)
    if slug is None:
        raise ToolError("name must yield a slug matching ^[a-z][a-z0-9-]*$")
    if not args.summary.strip():
        raise ToolError("summary must not be blank")

    skill_dir = _skill_dir(ctx.principal.workspace_id)
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = (skill_dir / f"{slug}.md").resolve()
    if md_path.parent != skill_dir:
        raise ToolError("invalid skill name")
    if md_path.exists():
        raise ToolError(f"skill already exists: {slug}")

    md_path.write_text(
        _skill_markdown(
            name=slug, description=args.summary.strip(), system_prompt=args.system_prompt
        ),
        encoding="utf-8",
    )

    loader = _loader_for(ctx.principal.workspace_id)
    if slug not in loader.registry:  # pragma: no cover — written file must parse
        raise ToolError("created skill did not load")
    return _Envelope(_meta_to_dict(loader.get(slug)))


# ---------------------------------------------------------------------------
# bsvibe_skills_update
# ---------------------------------------------------------------------------
class SkillsUpdateInput(BaseModel):
    """Mirror of :class:`SkillUpdate` (REST) + the path arg.

    Only the editable body fields are mutable: ``summary`` (manifest
    ``description``) and ``system_prompt`` (Markdown body). The slug /
    ``name`` is immutable — same contract as the REST surface.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    system_prompt: str = Field(min_length=1, max_length=100_000)


async def _h_update(args: SkillsUpdateInput, ctx: ToolContext) -> Any:
    loader = _loader_for(ctx.principal.workspace_id)
    if args.name not in loader.registry:
        raise ToolError(f"skill not found: {args.name}")
    if not args.summary.strip():
        raise ToolError("summary must not be blank")

    existing = loader.get(args.name)
    skill_dir = _skill_dir(ctx.principal.workspace_id)
    md_path = (skill_dir / f"{args.name}.md").resolve()
    if md_path.parent != skill_dir or not md_path.is_file():
        raise ToolError(f"skill not found: {args.name}")

    md_path.write_text(
        _skill_markdown(
            name=existing.name,
            description=args.summary.strip(),
            system_prompt=args.system_prompt,
            version=existing.version,
        ),
        encoding="utf-8",
    )

    loader = _loader_for(ctx.principal.workspace_id)
    if args.name not in loader.registry:  # pragma: no cover — rewritten file must parse
        raise ToolError("updated skill did not load")
    return _Envelope(_meta_to_dict(loader.get(args.name)))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_skills_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_skills_list",
            description=(
                "List skills loaded for the active workspace (the same set the "
                "PWA's Skills library surfaces)."
            ),
            input_schema=SkillsListInput,
            output_schema=_Envelope,
            handler=_h_list,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_skills_get",
            description=(
                "Fetch one skill's manifest + system_prompt body by name. "
                "Returns ToolError when the skill isn't loaded for the workspace."
            ),
            input_schema=SkillsGetInput,
            output_schema=_Envelope,
            handler=_h_get,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_skills_create",
            description=(
                "Create a skill — writes <skills_root>/<workspace_id>/<slug>.md "
                "with the manifest + Markdown body, slugifying `name` to a safe "
                "^[a-z][a-z0-9-]*$ filename. ToolError on collision (existing "
                "slug) or unsafe name. Mirrors the PWA SkillEditor 'Create' form."
            ),
            input_schema=SkillsCreateInput,
            output_schema=_Envelope,
            handler=_h_create,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.skills_create.invoked",
        )
    )
    registry.register(
        Tool(
            name="bsvibe_skills_update",
            description=(
                "Patch a skill's editable body fields (`summary` + "
                "`system_prompt`). The slug / `name` is immutable — a rename "
                "would mean a file rename, deferred to a later lift. Mirrors "
                "the PWA SkillEditor 'Save' affordance."
            ),
            input_schema=SkillsUpdateInput,
            output_schema=_Envelope,
            handler=_h_update,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.skills_update.invoked",
        )
    )


__all__ = ["register_skills_tools"]
