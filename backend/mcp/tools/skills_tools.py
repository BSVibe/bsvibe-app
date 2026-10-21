"""Skills tools — UI-parity surface (Lift D3c).

Wraps the per-workspace skills registry the PWA's
:mod:`apps/pwa/components/skills/{SkillsLibrary,SkillViewer,SkillEditor}`
view drives via ``/api/v1/skills``. Both go through
:mod:`backend.extensions.skill.store`, so the MCP and PWA paths see the same
skills in the workspace's store (files or rows, #929).

PWA exposes list / get / create / update — there is no "run" button, so
this lift ships only those four. ``invoke_skill`` is the agent loop's
runtime entry point, not a founder-visible surface.

Scopes follow the existing convention: ``mcp:read`` for list / get,
``mcp:write`` for create / update.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backend.extensions.skill import SkillLoader, SkillMeta
from backend.extensions.skill.store import (
    SkillConflict,
    SkillInvalid,
    SkillNotFound,
    create_workspace_skill,
    update_workspace_skill,
    workspace_skill_loader,
)
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry


class _Envelope(RootModel[Any]):
    """Permissive output envelope — preserves the natural JSON shape."""


async def _loader_for_ctx(ctx: ToolContext) -> SkillLoader:
    """Per-workspace loader. Tests inject one via ``ctx.extras["skill_loader"]``."""
    cached = ctx.extras.get("skill_loader") if ctx.extras else None
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    return await workspace_skill_loader(ctx.principal.workspace_id)


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


# ---------------------------------------------------------------------------
# bsvibe_skills_list
# ---------------------------------------------------------------------------
class SkillsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _h_list(_args: SkillsListInput, ctx: ToolContext) -> Any:
    loader = await _loader_for_ctx(ctx)
    return _Envelope([_meta_to_dict(m) for m in loader.registry.values()])


# ---------------------------------------------------------------------------
# bsvibe_skills_get
# ---------------------------------------------------------------------------
class SkillsGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)


async def _h_get(args: SkillsGetInput, ctx: ToolContext) -> Any:
    loader = await _loader_for_ctx(ctx)
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
    try:
        meta = await create_workspace_skill(
            ctx.principal.workspace_id,
            name=args.name,
            summary=args.summary,
            system_prompt=args.system_prompt,
        )
    except (SkillInvalid, SkillConflict) as exc:
        raise ToolError(str(exc)) from exc
    return _Envelope(_meta_to_dict(meta))


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
    try:
        meta = await update_workspace_skill(
            ctx.principal.workspace_id,
            name=args.name,
            summary=args.summary,
            system_prompt=args.system_prompt,
        )
    except SkillNotFound as exc:
        raise ToolError(f"skill not found: {args.name}") from exc
    except SkillInvalid as exc:
        raise ToolError(str(exc)) from exc
    return _Envelope(_meta_to_dict(meta))


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
                "Create a skill — writes the workspace's <slug>.md skill "
                "with the manifest + Markdown body, slugifying `name` to a safe "
                "^[a-z][a-z0-9-]*$ name. ToolError on collision (existing "
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
