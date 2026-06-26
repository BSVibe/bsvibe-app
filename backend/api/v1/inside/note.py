"""``GET /api/v1/inside/note`` — read ONE vault note's content (R12).

The deliverable report's "추가한 지식" / "참고한 지식" chips deep-link here so the
founder can SEE the actual note a run wrote or consulted — the hub-capped graph
view drops fresh low-degree notes, so this is how a just-written note becomes
verifiable. Strictly read-only, workspace-scoped via the SAME per-workspace
storage the observation list reads; a path outside the note dirs, a traversal
attempt, or a missing file is a calm 404 (never leaks existence across the
workspace boundary).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.knowledge.graph.markdown_utils import body_after_frontmatter, extract_title
from backend.knowledge.graph.storage import StorageBackend

from ._dependencies import build_inside_storage
from ._schemas import NoteResponse

router = APIRouter()

# Vault subdirs that hold founder-viewable notes (mirrors the graph_store scan
# dirs). A note path must live under one of these — never ``.bsage/`` internals.
_NOTE_DIRS = frozenset(
    {
        "garden",
        "seeds",
        "ideas",
        "insights",
        "projects",
        "people",
        "events",
        "tasks",
        "facts",
        "preferences",
    }
)


def _is_note_path(path: str) -> bool:
    """A vault-relative ``<note-dir>/.../<slug>.md`` path, no traversal."""
    if not path.endswith(".md") or path.startswith("/"):
        return False
    parts = PurePosixPath(path).parts
    return bool(parts) and ".." not in parts and parts[0] in _NOTE_DIRS


async def _exists(storage: StorageBackend, path: str) -> bool:
    """``storage.exists`` but a traversal attempt (ValueError) reads as absent."""
    try:
        return await storage.exists(path)
    except ValueError:
        return False


@router.get("/note")
async def get_note(
    storage: Annotated[StorageBackend, Depends(build_inside_storage)],
    path: Annotated[
        str, Query(description="vault-relative note path, e.g. garden/seedling/settle-x.md")
    ],
) -> NoteResponse:
    """One note's title + body (YAML frontmatter stripped), scoped to the caller's
    workspace vault. 404 for a path outside the note dirs, a traversal attempt, or
    a missing file — never leaking existence across the workspace boundary."""
    if not _is_note_path(path) or not await _exists(storage, path):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found")
    try:
        text = await storage.read(path)
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="note not found") from exc
    return NoteResponse(
        path=path,
        title=extract_title(text) or PurePosixPath(path).stem.replace("-", " ").strip(),
        content=body_after_frontmatter(text).strip(),
    )


__all__ = ["router"]
