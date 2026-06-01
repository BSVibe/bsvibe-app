"""Product files — a lazy, per-directory browser over the product's git main.

Replaces the per-deliverable flat ``artifact_refs`` list (which only ever
showed the files a single run touched and never scaled to a real repo). The
tree is fetched one directory at a time so a large product repo stays cheap to
browse, and content is read from the product main checkout (the shipped state).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db_session, get_workspace_id
from backend.config import get_settings
from backend.storage.artifact_store import LocalFilesystemArtifactStore
from backend.storage.product_workspace import list_product_tree

from ._helpers import _MAX_FILE_BYTES, _looks_binary, _resolve_product_in_workspace
from ._schemas import FileTreeEntryResponse, ProductFileContentResponse

router = APIRouter()


@router.get("/{product_id}/files")
async def list_product_files(
    product_id: uuid.UUID,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    path: str = "",
) -> list[FileTreeEntryResponse]:
    """List the immediate children of ``path`` (default root) in the product's
    ``main`` tree. One level only — the browser fetches each directory on
    demand. An uninitialised product / unsafe path yields ``[]``."""
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    entries = await list_product_tree(product_id, path)
    return [FileTreeEntryResponse(name=e.name, path=e.path, kind=e.kind) for e in entries]


@router.get("/{product_id}/files/content")
async def get_product_file_content(
    product_id: uuid.UUID,
    path: str,
    workspace_id: Annotated[uuid.UUID, Depends(get_workspace_id)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ProductFileContentResponse:
    """Serve one file's CONTENT from the product's ``main`` checkout, read-only.

    Reuses the centralized traversal guard by rooting a
    :class:`LocalFilesystemArtifactStore` at ``product_workspace_root`` and
    keying it by ``product_id`` (resolves ``<root>/<product_id>/<path>``). A
    traversal / absolute path, a directory, or a missing file all 404 calmly —
    never a leak, never a 500. Binary files yield a short note; text is capped
    at 256 KiB with ``truncated: true`` past the cap."""
    await _resolve_product_in_workspace(session, product_id, workspace_id)
    store = LocalFilesystemArtifactStore(Path(get_settings().product_workspace_root))
    try:
        raw = store.read_bytes(product_id, path)
    except (ValueError, FileNotFoundError, IsADirectoryError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found") from exc
    if _looks_binary(raw):
        return ProductFileContentResponse(
            path=path, content=f"Binary file, {len(raw)} bytes — not shown.", binary=True
        )
    truncated = len(raw) > _MAX_FILE_BYTES
    text = raw[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return ProductFileContentResponse(path=path, content=text, truncated=truncated)


__all__ = ["router"]
