"""Knowledge tools — vault-direct read surface for the embedded MCP server.

The knowledge surface in bsvibe-app is filesystem-backed (one vault per
``(region, workspace_id)``) and there are no HTTP endpoints over it
today. These tools read directly off the workspace-scoped
:class:`backend.knowledge.graph.vault.Vault` — never another workspace's
vault, because we resolve the root from the verified principal's
``workspace_id`` claim.

D2 ships a minimal vault read surface — list_recent + get_note + list_tags.
Hybrid search + graph traversal land in follow-up lifts once the v8
:class:`Knowledge` facade has a search implementation wired in.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.knowledge.graph.vault import Vault
from backend.mcp.api import Tool, ToolContext, ToolError, ToolRegistry
from backend.mcp.tools._helpers import vault_root_for, workspace_region


class _PermissiveModel(BaseModel):
    """Output base — preserves handler-supplied extras on the wire."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# Vault factory — one Vault per tool call, rooted at the principal's workspace.
# ---------------------------------------------------------------------------
async def _vault_for_call(ctx: ToolContext) -> Vault:
    region = await workspace_region(ctx.session, ctx.principal.workspace_id)
    root = vault_root_for(region=region, workspace_id=ctx.principal.workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    return Vault(root)


_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_\-/]+)")


def _extract_tags(text: str) -> list[str]:
    """Return unique ``#tag`` tokens from the note body."""
    return sorted({m.group(1) for m in _TAG_RE.finditer(text)})


def _excerpt(text: str, *, max_chars: int = 240) -> str:
    """Plain-text preview of a note body — first non-empty content."""
    body = text.strip()
    if not body:
        return ""
    if len(body) <= max_chars:
        return body
    return body[:max_chars].rstrip() + "…"


def _path_relative_to_vault(vault: Vault, p: Path) -> str:
    """Return the vault-relative POSIX path string."""
    return p.resolve().relative_to(vault.root).as_posix()


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------
class ListRecentInput(BaseModel):
    limit: int = Field(20, ge=1, le=200)
    subdir: str = Field(
        "garden",
        max_length=128,
        description="Vault subdirectory to scan (e.g. 'garden', 'seeds').",
    )


class NoteSummary(_PermissiveModel):
    path: str
    excerpt: str = ""
    tags: list[str] = Field(default_factory=list)


class ListRecentOutput(_PermissiveModel):
    subdir: str
    total: int
    notes: list[NoteSummary]


async def _h_list_recent(args: ListRecentInput, ctx: ToolContext) -> Any:
    vault = await _vault_for_call(ctx)
    files = await vault.read_notes(args.subdir)
    files = files[-args.limit :] if len(files) > args.limit else files
    notes: list[NoteSummary] = []
    for f in files:
        try:
            content = await vault.read_note_content(f)
        except OSError:
            continue
        notes.append(
            NoteSummary(
                path=_path_relative_to_vault(vault, f),
                excerpt=_excerpt(content),
                tags=_extract_tags(content),
            )
        )
    return ListRecentOutput(subdir=args.subdir, total=len(notes), notes=notes)


# ---------------------------------------------------------------------------
# get_note
# ---------------------------------------------------------------------------
class GetNoteInput(BaseModel):
    path: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Vault-relative POSIX path (e.g. 'garden/topic/note.md').",
    )


class GetNoteOutput(BaseModel):
    path: str
    content: str
    tags: list[str] = Field(default_factory=list)


async def _h_get_note(args: GetNoteInput, ctx: ToolContext) -> Any:
    vault = await _vault_for_call(ctx)
    try:
        target = vault.resolve_path(args.path)
    except Exception as exc:  # noqa: BLE001 — boundary
        raise ToolError(f"invalid vault path: {args.path}") from exc
    if not target.is_file():
        raise ToolError(f"note not found: {args.path}")
    content = await vault.read_note_content(target)
    return GetNoteOutput(path=args.path, content=content, tags=_extract_tags(content))


# ---------------------------------------------------------------------------
# search_knowledge — naive substring scan across the configured subdir
# ---------------------------------------------------------------------------
class SearchKnowledgeInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    subdir: str = Field("garden", max_length=128)
    limit: int = Field(10, ge=1, le=50)


class SearchHit(_PermissiveModel):
    path: str
    excerpt: str = ""


class SearchKnowledgeOutput(_PermissiveModel):
    query: str
    subdir: str
    total: int
    results: list[SearchHit]


async def _h_search_knowledge(args: SearchKnowledgeInput, ctx: ToolContext) -> Any:
    vault = await _vault_for_call(ctx)
    files = await vault.read_notes(args.subdir)
    needle = args.query.lower()
    hits: list[SearchHit] = []
    for f in files:
        try:
            content = await vault.read_note_content(f)
        except OSError:
            continue
        if needle in content.lower():
            hits.append(
                SearchHit(
                    path=_path_relative_to_vault(vault, f),
                    excerpt=_excerpt(content),
                )
            )
            if len(hits) >= args.limit:
                break
    return SearchKnowledgeOutput(
        query=args.query, subdir=args.subdir, total=len(hits), results=hits
    )


# ---------------------------------------------------------------------------
# list_tags — aggregate #tag frequency across the configured subdir
# ---------------------------------------------------------------------------
class ListTagsInput(BaseModel):
    subdir: str = Field("garden", max_length=128)
    limit: int = Field(50, ge=1, le=500)


class TagCount(BaseModel):
    tag: str
    count: int


class ListTagsOutput(_PermissiveModel):
    subdir: str
    total: int
    tags: list[TagCount]


async def _h_list_tags(args: ListTagsInput, ctx: ToolContext) -> Any:
    vault = await _vault_for_call(ctx)
    files = await vault.read_notes(args.subdir)
    counts: dict[str, int] = {}
    for f in files:
        try:
            content = await vault.read_note_content(f)
        except OSError:
            continue
        for tag in _extract_tags(content):
            counts[tag] = counts.get(tag, 0) + 1
    pairs = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: args.limit]
    return ListTagsOutput(
        subdir=args.subdir,
        total=len(pairs),
        tags=[TagCount(tag=t, count=c) for t, c in pairs],
    )


# ---------------------------------------------------------------------------
# create_note — write a seed under seeds/mcp/<slug>.md
# ---------------------------------------------------------------------------
class CreateNoteInput(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field("", max_length=200_000)
    tags: list[str] = Field(default_factory=list, max_length=32)


class CreateNoteOutput(BaseModel):
    seed_path: str
    bytes_written: int


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(title: str) -> str:
    out = _SLUG_RE.sub("-", title.lower()).strip("-")
    return out or "note"


async def _h_create_note(args: CreateNoteInput, ctx: ToolContext) -> Any:
    vault = await _vault_for_call(ctx)
    seeds_dir = vault.resolve_path("seeds/mcp")
    seeds_dir.mkdir(parents=True, exist_ok=True)
    slug = _slugify(args.title)
    target = seeds_dir / f"{slug}.md"
    # Don't overwrite an existing seed — append a numeric suffix.
    counter = 1
    while target.exists():
        target = seeds_dir / f"{slug}-{counter}.md"
        counter += 1
    tag_line = " ".join(f"#{t}" for t in args.tags if t)
    parts = [f"# {args.title}", ""]
    if tag_line:
        parts.append(tag_line)
        parts.append("")
    parts.append(args.content.rstrip())
    body = "\n".join(parts).rstrip() + "\n"
    target.write_text(body, encoding="utf-8")
    return CreateNoteOutput(
        seed_path=_path_relative_to_vault(vault, target),
        bytes_written=len(body.encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_knowledge_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_knowledge_list_recent",
            description="List recent notes from a vault subdirectory (default: 'garden').",
            input_schema=ListRecentInput,
            output_schema=ListRecentOutput,
            handler=_h_list_recent,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_knowledge_get_note",
            description="Read a single vault note by its vault-relative POSIX path.",
            input_schema=GetNoteInput,
            output_schema=GetNoteOutput,
            handler=_h_get_note,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_knowledge_search",
            description=(
                "Case-insensitive substring search across the notes of a vault "
                "subdirectory. Returns up to `limit` matching notes with excerpts."
            ),
            input_schema=SearchKnowledgeInput,
            output_schema=SearchKnowledgeOutput,
            handler=_h_search_knowledge,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_knowledge_list_tags",
            description="Aggregate `#tag` frequency across a vault subdirectory.",
            input_schema=ListTagsInput,
            output_schema=ListTagsOutput,
            handler=_h_list_tags,
            required_scopes=("mcp:read",),
        )
    )
    registry.register(
        Tool(
            name="bsvibe_knowledge_create_note",
            description=(
                "Submit a seed note under `seeds/mcp/`. The ingest pipeline picks "
                "it up and classifies / links it against the existing vault."
            ),
            input_schema=CreateNoteInput,
            output_schema=CreateNoteOutput,
            handler=_h_create_note,
            required_scopes=("mcp:write",),
            audit_event="bsvibe.mcp.create_note.invoked",
        )
    )


__all__ = ["register_knowledge_tools"]
