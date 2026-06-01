"""``GET /api/v1/inside/observations`` — the recent garden observation list.

Strictly read-only adapter over the per-workspace vault storage. Garden notes
live under ``garden/<maturity>/<slug>.md`` (the SettleWorker writes
``garden/seedling/...`` via the GardenWriter) — we list them straight off the
FS-as-SoT store the canonicalization index reads.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.knowledge.graph.markdown_utils import (
    body_after_frontmatter,
    extract_frontmatter,
    extract_title,
)
from backend.knowledge.graph.storage import StorageBackend

from ._dependencies import build_inside_storage
from ._helpers import (
    _DEFAULT_OBSERVATION_LIMIT,
    _MAX_OBSERVATION_LIMIT,
    _excerpt,
)
from ._schemas import ObservationResponse

router = APIRouter()


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


__all__ = ["router"]
