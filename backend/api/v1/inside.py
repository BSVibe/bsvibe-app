"""/api/v1/inside — the founder's read-only window into the knowledge graph.

The trust ratchet (Workflow §5) accumulates knowledge as the AI verifies work:
the SettleWorker deposits raw *garden observations*, and the canonicalization
promoter graduates recurring patterns into *canonical anchors* — the settled
``concepts/active/<id>.md`` "wall". The PWA "Inside" moment is the founder
peeking at what the AI has learned; this router is the backend surface that
moment reads from. It exposes two calm, read-only lists:

* ``GET /inside/concepts`` — the canonical anchors (settled concepts), sourced
  through :meth:`InMemoryCanonicalizationIndex.list_active_concepts` (the SAME
  vault-derived enumeration the canonicalization queue uses, FS-as-SoT).
* ``GET /inside/observations`` — the recent garden observation notes the
  SettleWorker writes (the unpromoted raw samples), newest first.

Workspace isolation is structural — exactly the boundary
:mod:`backend.api.v1.decisions` enforces. Each request builds an index +
storage rooted at ``<knowledge_vault_root>/<region>/<workspace_id>/`` (via the
shared :func:`backend.api.v1.decisions._vault_root` helper), so another
workspace's vault is simply not there: a concept or observation outside the
caller's workspace is never enumerated. No DB table is involved (the vault is
the source of truth) and there is no write path here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from backend.api.deps import get_workspace_id
from backend.api.v1.decisions import _vault_root
from backend.knowledge.canonicalization.index import InMemoryCanonicalizationIndex
from backend.knowledge.graph.markdown_utils import (
    body_after_frontmatter,
    extract_frontmatter,
    extract_title,
)
from backend.knowledge.graph.storage import FileSystemStorage, StorageBackend

router = APIRouter()

# Conservative caps — the Inside surface is a calm snapshot, not a data dump.
_DEFAULT_CONCEPT_LIMIT = 50
_MAX_CONCEPT_LIMIT = 200
_DEFAULT_OBSERVATION_LIMIT = 25
_MAX_OBSERVATION_LIMIT = 100

# Excerpt cap — a short, founder-legible blurb, not the full note body.
_EXCERPT_CHARS = 200


async def build_inside_storage(
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
) -> StorageBackend:
    """Read-only vault storage rooted at the caller's per-workspace vault.

    Same per-workspace root the canonicalization queue + promotion pipeline
    write to (``<knowledge_vault_root>/<region>/<workspace_id>/`` via
    :func:`backend.api.v1.decisions._vault_root`), so the anchors and garden
    observations read here are exactly the ones the trust ratchet built for
    THIS workspace — a vault outside it is not addressable.

    Overridable in tests via ``app.dependency_overrides`` to point at a
    fixture vault.
    """
    vault_root = _vault_root(workspace_id)
    vault_root.mkdir(parents=True, exist_ok=True)
    return FileSystemStorage(vault_root)


async def build_inside_index(
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
) -> InMemoryCanonicalizationIndex:
    """Vault-derived canonicalization index for listing canonical anchors.

    Rebuilds from the workspace vault markdown alone (Handoff §10) — a pure
    read of the FS-as-SoT concept registry. Rooted at the same storage as
    :func:`build_inside_storage` so the index never sees another workspace's
    concepts.
    """
    index = InMemoryCanonicalizationIndex()
    await index.initialize(storage)
    return index


class ConceptResponse(BaseModel):
    """One canonical anchor (settled concept) on the founder's "wall".

    ``id`` is the concept's vault stem (``concepts/active/<id>.md``); ``name``
    is its display title (the note's H1). ``alias_count`` is the cheap
    connectedness signal available without a graph traversal — how many variant
    spellings resolve onto this anchor. ``summary`` is a short excerpt of the
    concept body (empty for a freshly-promoted anchor that carries only its
    title).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    summary: str
    aliases: list[str]
    alias_count: int
    created_at: datetime
    updated_at: datetime


class ObservationResponse(BaseModel):
    """One recent garden observation (an unpromoted settle note).

    These are the raw, monotonically-accumulating samples the SettleWorker
    deposits per verified work step (the *learning* half of the ratchet).
    ``id`` is the note's vault path; ``title`` its H1; ``excerpt`` a short blurb
    of the body; ``tags`` the note's content + structural tags; ``captured_at``
    the deposit date the writer stamped.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    excerpt: str
    tags: list[str]
    captured_at: str | None = None


def _excerpt(body: str) -> str:
    """First non-empty body line (after the H1), truncated for a calm blurb."""
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        return line[:_EXCERPT_CHARS]
    return ""


@router.get("/concepts")
async def list_concepts(
    index: Annotated[InMemoryCanonicalizationIndex, Depends(build_inside_index)],
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
    limit: Annotated[int, Query(ge=1, le=_MAX_CONCEPT_LIMIT)] = _DEFAULT_CONCEPT_LIMIT,
) -> list[ConceptResponse]:
    """List the workspace's canonical anchors (active concepts), newest first.

    Sourced through :meth:`InMemoryCanonicalizationIndex.list_active_concepts`
    — the existing vault-derived enumeration, NOT a new engine method. Sorted
    by ``updated_at`` so the most recently-settled anchors lead. The concept
    body (if any) is read to build a short excerpt; a freshly-promoted anchor
    carries only its title and yields an empty summary.
    """
    concepts = await index.list_active_concepts()
    concepts.sort(key=lambda c: c.updated_at, reverse=True)
    out: list[ConceptResponse] = []
    for concept in concepts[:limit]:
        summary = ""
        if await storage.exists(concept.path):
            text = await storage.read(concept.path)
            summary = _excerpt(body_after_frontmatter(text))
        out.append(
            ConceptResponse(
                id=concept.concept_id,
                name=concept.display,
                summary=summary,
                aliases=list(concept.aliases),
                alias_count=len(concept.aliases),
                created_at=concept.created_at,
                updated_at=concept.updated_at,
            )
        )
    return out


@router.get("/observations")
async def list_observations(
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
    limit: Annotated[int, Query(ge=1, le=_MAX_OBSERVATION_LIMIT)] = _DEFAULT_OBSERVATION_LIMIT,
) -> list[ObservationResponse]:
    """List recent garden observation notes (raw settle notes), newest first.

    Garden notes live under ``garden/<maturity>/<slug>.md`` (the SettleWorker
    writes ``garden/seedling/...`` via the GardenWriter). Read straight off the
    vault storage — the same FS-as-SoT store the canonicalization index reads —
    and sorted by the writer-stamped ``captured_at`` (path as a stable
    tiebreaker), so the freshest observations lead.
    """
    paths = await storage.list_files("garden", "*.md")
    rows: list[tuple[str, str | None, ObservationResponse]] = []
    for path in paths:
        text = await storage.read(path)
        fm = extract_frontmatter(text)
        captured_at = fm.get("captured_at")
        captured_str = captured_at if isinstance(captured_at, str) else None
        rows.append(
            (
                path,
                captured_str,
                ObservationResponse(
                    id=path,
                    title=extract_title(text) or PurePosixPath(path).stem,
                    excerpt=_excerpt(body_after_frontmatter(text)),
                    tags=[str(t) for t in (fm.get("tags") or [])],
                    captured_at=captured_str,
                ),
            )
        )
    # Newest first: captured_at descending, then path descending as a stable
    # tiebreaker (notes without a date sort last).
    rows.sort(key=lambda r: (r[1] or "", r[0]), reverse=True)
    return [resp for _, _, resp in rows[:limit]]
