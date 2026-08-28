"""Embedding backfill trigger — UI-parity write surface for the vector index.

Mirrors ``POST /api/v1/inside/reindex-embeddings``. The backfill exists because
the note vector index is populated event-driven (the settle promote hook), so a
corpus only self-heals when knowledge activity happens to occur. Until this tool
the sole deliberate trigger was the REST route, and its callers — measured across
the repo — were tests: an away founder holding an MCP client could not fire it.

The reconcile logic is NOT copied here. ``reconcile_embeddings`` is the one chain
REST, the settle hook and this tool share, and the skip decision stays inside
``embed_and_store_note`` — the only function that knows the embedded text. A
second copy of "what text represents this note" is the exact drift that keyed
1,724 prod vectors to a work-log line (#837/#838).

The vault is resolved through ``workspace_region`` + ``vault_root_for``, i.e. the
workspace's OWN region — the same boundary the settle hook writes through. Using
the deployment default region instead would read an empty directory and report
``scanned: 0`` as a success.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.mcp.api import Tool, ToolContext, ToolRegistry
from backend.mcp.tools._helpers import vault_root_for, workspace_region


class ReindexEmbeddingsInput(BaseModel):
    """No arguments — the workspace is the principal's."""

    model_config = ConfigDict(extra="forbid")


class ReindexEmbeddingsOutput(BaseModel):
    """1:1 with the REST ``ReindexEmbeddingsResponse``."""

    model_config = ConfigDict(extra="forbid")

    scanned: int
    embedded: int
    already: int
    disabled: bool


async def _h_reindex(_args: ReindexEmbeddingsInput, ctx: ToolContext) -> Any:
    # Imported lazily to keep the MCP tool-import graph off the embedding stack
    # (same shape as the settle hook's reconcile import).
    from backend.config import get_settings  # noqa: PLC0415
    from backend.knowledge.graph.vault import Vault  # noqa: PLC0415
    from backend.knowledge.retrieval.embedder_resolution import (  # noqa: PLC0415
        resolve_knowledge_embedder,
    )
    from backend.knowledge.retrieval.reconcile import reconcile_embeddings  # noqa: PLC0415
    from backend.knowledge.retrieval.storage.pg import PgNoteVectorBackend  # noqa: PLC0415

    workspace_id = ctx.principal.workspace_id
    embedder = resolve_knowledge_embedder(get_settings())
    if not embedder.enabled or embedder.model is None:
        # Honest "nothing to report" rather than a fabricated zero-scan success.
        return ReindexEmbeddingsOutput(scanned=0, embedded=0, already=0, disabled=True)

    region = await workspace_region(ctx.session, workspace_id)
    vault = Vault(vault_root_for(region=region, workspace_id=workspace_id))
    backend = PgNoteVectorBackend(
        ctx.session, workspace_id=workspace_id, embedding_model=embedder.model
    )
    result = await reconcile_embeddings(vault, embedder, backend)
    await ctx.session.commit()
    return ReindexEmbeddingsOutput(
        scanned=result.scanned,
        embedded=result.embedded,
        already=result.already,
        disabled=result.disabled,
    )


def register_reindex_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="bsvibe_knowledge_reindex_embeddings",
            description=(
                "Backfill this workspace's note vector index — embed every knowledge "
                "note (garden + concepts) whose stored vector is missing, built by a "
                "different model, or built from different text. Mirrors "
                "`POST /api/v1/inside/reindex-embeddings`. Idempotent: a second pass "
                "re-embeds nothing and returns `embedded: 0`. Returns `disabled: true` "
                "when the deployment configures no embedding model."
            ),
            input_schema=ReindexEmbeddingsInput,
            output_schema=ReindexEmbeddingsOutput,
            handler=_h_reindex,
            required_scopes=("mcp:write",),
        )
    )


__all__ = ["register_reindex_tools"]
