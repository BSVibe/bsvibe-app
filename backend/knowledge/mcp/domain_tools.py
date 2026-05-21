"""First-class :class:`bsage.mcp.api.Tool` definitions for the 9 static
domain MCP tools (``search_knowledge``, ``get_note``,
``get_graph_context``, ``list_recent``, ``list_by_tag``, ``list_tags``,
``browse_communities``, ``browse_entity``, ``create_note``).

Each tool ships with explicit Pydantic ``input_schema`` /
``output_schema`` models — the JSON Schema published over the MCP wire
is whatever ``model_json_schema()`` produces. Handlers delegate to the
existing transport-agnostic service layer in
:mod:`bsage.gateway.mcp_tools`; CLI / REST / MCP all share the same
service implementations.

``required_permission`` is ``None`` for every static tool: the gateway
already authenticates the request (the Streamable HTTP transport
resolves the principal from the ``Authorization`` header per-request —
see ``bsage/mcp/streamable_http.py``), and the contract is
"any authenticated principal may call any read tool."  ``create_note``
adds an ``audit_event`` because it's the one mutating tool in this
catalog. (Tier 5 Phase 3a renamed the legacy ``required_scopes`` list to
the OpenFGA-backed ``required_permission`` dot string — see
:mod:`bsage.mcp.api`.)
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.gateway import mcp_tools as service
from backend.knowledge.mcp.api import Tool, ToolContext, ToolRegistry


class _PermissiveModel(BaseModel):
    """Output-schema base — preserve handler-supplied extras on the wire."""

    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# search_knowledge
# ---------------------------------------------------------------------------
class SearchKnowledgeInput(BaseModel):
    query: str = Field(..., min_length=1, description="Search query")
    top_k: int = Field(10, ge=1, le=50)


class SearchHit(_PermissiveModel):
    title: str = ""
    path: str = ""
    preview: str = ""
    score: float = 0.0
    tags: list[str] = Field(default_factory=list)


class SearchKnowledgeOutput(_PermissiveModel):
    query: str
    results: list[SearchHit]


async def _h_search_knowledge(args: SearchKnowledgeInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.search_knowledge(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# get_note
# ---------------------------------------------------------------------------
class GetNoteInput(BaseModel):
    path: str = Field(..., description="Vault-relative file path")


class GetNoteOutput(BaseModel):
    path: str
    content: str


async def _h_get_note(args: GetNoteInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.get_note(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# get_graph_context
# ---------------------------------------------------------------------------
class GetGraphContextInput(BaseModel):
    topic: str
    max_hops: int = Field(2, ge=1, le=5)
    top_k: int = Field(10, ge=1, le=50)


class GetGraphContextOutput(BaseModel):
    topic: str
    context: str
    has_results: bool


async def _h_get_graph_context(args: GetGraphContextInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.get_graph_context(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------
class ListRecentInput(BaseModel):
    """No fields — the service currently ignores any extra payload."""

    model_config = ConfigDict(extra="allow")


class ListRecentOutput(_PermissiveModel):
    total: int
    categories: dict[str, list[dict[str, Any]]]


async def _h_list_recent(args: ListRecentInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.list_recent(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# list_by_tag
# ---------------------------------------------------------------------------
class ListByTagInput(BaseModel):
    tags: list[str]
    match: Literal["any", "all"] = "any"
    top_k: int = Field(50, ge=1, le=500)


class ListByTagOutput(_PermissiveModel):
    tags: list[str]
    match: str
    total: int
    results: list[dict[str, Any]]


async def _h_list_by_tag(args: ListByTagInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.list_by_tag(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# list_tags
# ---------------------------------------------------------------------------
class ListTagsInput(BaseModel):
    threshold: int = Field(3, ge=1)


class ListTagsOutput(_PermissiveModel):
    threshold: int
    primary: list[dict[str, Any]]
    long_tail: list[dict[str, Any]]
    total_unique: int


async def _h_list_tags(args: ListTagsInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.list_tags(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# browse_communities
# ---------------------------------------------------------------------------
class BrowseCommunitiesInput(BaseModel):
    min_size: int = Field(2, ge=1)


class BrowseCommunitiesOutput(_PermissiveModel):
    communities: list[dict[str, Any]]
    total: int


async def _h_browse_communities(args: BrowseCommunitiesInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.browse_communities(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# browse_entity
# ---------------------------------------------------------------------------
class BrowseEntityInput(BaseModel):
    name: str = Field(..., description="Bare entity name (no [[ ]] brackets).")


class BrowseEntityOutput(_PermissiveModel):
    name: str
    found: bool


async def _h_browse_entity(args: BrowseEntityInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.browse_entity(ctx.state, args.model_dump())


# ---------------------------------------------------------------------------
# create_note  (mutating — audit_event set)
# ---------------------------------------------------------------------------
class CreateNoteInput(BaseModel):
    title: str
    content: str = ""
    source: str = "mcp"
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateNoteOutput(_PermissiveModel):
    seed_path: str
    submitted_at: str
    notes_created: int
    notes_updated: int
    compiler_available: bool


async def _h_create_note(args: CreateNoteInput, ctx: ToolContext) -> dict[str, Any]:
    return await service.create_note(ctx.state, args.model_dump(), principal=ctx.user)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_DOMAIN_TOOLS: list[Tool] = [
    Tool(
        name="search_knowledge",
        description=("Semantic search across BSage vault notes. Falls back to full-text search."),
        input_schema=SearchKnowledgeInput,
        output_schema=SearchKnowledgeOutput,
        handler=_h_search_knowledge,
    ),
    Tool(
        name="get_note",
        description=("Read a vault file by relative path (e.g. garden/seedling/foo.md)."),
        input_schema=GetNoteInput,
        output_schema=GetNoteOutput,
        handler=_h_get_note,
    ),
    Tool(
        name="get_graph_context",
        description="Knowledge-graph BFS context for a topic.",
        input_schema=GetGraphContextInput,
        output_schema=GetGraphContextOutput,
        handler=_h_get_graph_context,
    ),
    Tool(
        name="list_recent",
        description=(
            "Vault catalog grouped by maturity (seedling/budding/"
            "evergreen). For 'show me all my X' partition queries, "
            "prefer list_by_tag."
        ),
        input_schema=ListRecentInput,
        output_schema=ListRecentOutput,
        handler=_h_list_recent,
    ),
    Tool(
        name="list_by_tag",
        description=(
            "Notes carrying one or more of the given tags. Use for "
            "partition queries like 'all my project notes' "
            "(tags: ['project']). match='all' for AND, 'any' (default) "
            "for OR."
        ),
        input_schema=ListByTagInput,
        output_schema=ListByTagOutput,
        handler=_h_list_by_tag,
    ),
    Tool(
        name="list_tags",
        description=(
            "All tags in the vault sorted by frequency. Splits into a "
            "primary list (count >= threshold) and a long_tail list so "
            "the dominant topic vocabulary stays legible."
        ),
        input_schema=ListTagsInput,
        output_schema=ListTagsOutput,
        handler=_h_list_tags,
    ),
    Tool(
        name="browse_communities",
        description=(
            "Louvain communities of the vault graph — emergent topic "
            "clusters with auto-generated labels. Navigate by 'topic "
            "neighbourhood' instead of by folder."
        ),
        input_schema=BrowseCommunitiesInput,
        output_schema=BrowseCommunitiesOutput,
        handler=_h_browse_communities,
    ),
    Tool(
        name="browse_entity",
        description=(
            "Backlinks + outgoing links + auto-stub flag for a single "
            "[[Name]] entity. Used to follow a wikilink and see the "
            "graph neighbourhood of a person/tool/concept/project."
        ),
        input_schema=BrowseEntityInput,
        output_schema=BrowseEntityOutput,
        handler=_h_browse_entity,
    ),
    Tool(
        name="create_note",
        description=(
            "Submit a note for ingestion — writes a seed and lets BSage's "
            "IngestCompiler classify and link it against existing vault "
            "content. The compiler decides note_type/tags/links; "
            "client-supplied tags and links are passed through as hints "
            "only."
        ),
        input_schema=CreateNoteInput,
        output_schema=CreateNoteOutput,
        handler=_h_create_note,
        audit_event="bsage.mcp.create_note.invoked",
    ),
]


def register_domain_tools(registry: ToolRegistry) -> None:
    """Register the 9 static domain tools into ``registry``."""
    for tool in _DOMAIN_TOOLS:
        registry.register(tool)


def domain_tool_names() -> list[str]:
    """Return the names of every domain tool — used for legacy aliases."""
    return [t.name for t in _DOMAIN_TOOLS]


__all__ = [
    "domain_tool_names",
    "register_domain_tools",
]
