"""Reading one artifact file out of a deliverable — one rule for REST and MCP.

The browser's file viewer and the ``bsvibe_deliverables_artifacts`` tool read
through the SAME function, so the tool cannot end up laxer than the page: the
ref whitelist, the traversal guard, the size cap and the binary refusal are
properties of the rule, not of either adapter.

형님 판단 2026-09-01 — the tool is offered with the REST constraints unchanged.
The axis that genuinely moves is the token: file content was reachable only
with a RUN-scoped token (``bsvibe_work_file_read``), and is now also reachable
with a workspace token — but only for a deliverable's own declared refs, as
text, capped.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.storage.artifact_store import ArtifactStore, LocalFilesystemArtifactStore
from backend.workflow.domain.repositories import DeliverableRepository
from backend.workflow.infrastructure.db import ExecutionRun
from backend.workflow.serialization.deliverable_views import (
    MAX_CONTENT_BYTES,
    ArtifactContentResponse,
    artifact_refs_of,
)

logger = structlog.get_logger(__name__)


def run_artifact_store() -> ArtifactStore:
    """The store rooted at the run-workspace root.

    ONE definition of where a run's files live. The REST dependency and the MCP
    tool both call this — a second copy of the root would be a place for the two
    surfaces to disagree about which directory they are reading. Settings are
    read per call so a test that repoints the root sees it take effect.
    """
    from backend.config import get_settings  # noqa: PLC0415

    settings = get_settings()
    return LocalFilesystemArtifactStore(Path(settings.run_workspace_root))


def _looks_binary(raw: bytes) -> bool:
    """Heuristic binary sniff: a NUL byte in the inspected prefix → binary.

    Mirrors git's own "is this a text file" test (a NUL in the first 8 KiB).
    Cheap, dependency-free, and deliberately conservative — a stray NUL makes
    us report metadata-only rather than dumping mojibake into a JSON string.
    """
    return b"\x00" in raw[:8192]


async def _read_from_product_main(
    session: AsyncSession, run_id: uuid.UUID, ref: str
) -> bytes | None:
    """Read ``ref`` from the run's product workspace main checkout, or ``None``.

    The W2 ship-time merge lands the run's files under
    ``<product_workspace_root>/<product_id>/`` (the product repo's main checkout)
    and then removes the per-run worktree. A reused
    :class:`LocalFilesystemArtifactStore` rooted at ``product_workspace_root`` and
    keyed by ``product_id`` resolves ``<root>/<product_id>/<ref>`` with the SAME
    centralized traversal guard. ``None`` when the run has no product_id (nothing
    to fall back to) or the file is genuinely absent — the caller maps it to 404.
    """
    run = await session.get(ExecutionRun, run_id)
    if run is None or run.product_id is None:
        return None
    from backend.storage.product_workspace import read_product_file  # noqa: PLC0415

    try:
        return await read_product_file(run.product_id, ref)
    except (ValueError, FileNotFoundError, IsADirectoryError):
        return None


async def read_deliverable_artifact(  # noqa: PLR0911 — each return is a distinct,
    # documented refusal (wrong workspace / not a declared ref / traversal / gone /
    # directory); collapsing them would lose the reason each one exists.
    *,
    deliverable_id: uuid.UUID,
    ref: str,
    workspace_id: uuid.UUID,
    session: AsyncSession,
    store: ArtifactStore,
    deliverables: DeliverableRepository,
) -> ArtifactContentResponse | None:
    """Read one artifact file's CONTENT, or ``None`` when it is not readable here.

    Every refusal collapses to ``None`` on purpose — the caller spells it (REST
    404, MCP ToolError) and neither surface can distinguish "wrong workspace"
    from "not a declared ref" from "file is gone". That is the point: existence
    is never leaked across the boundary.

    The constraints travel WITH the rule, so the MCP tool cannot be laxer than
    the browser:
      * workspace scope — the deliverable must belong to the caller's workspace;
      * ref whitelist — ``ref`` MUST be one of the deliverable's own
        ``payload.artifact_refs``; an arbitrary path is refused outright;
      * path traversal — the store's centralized guard refuses any ref that
        resolves outside the run dir (absolute path / ``../`` segment);
      * size — capped at 256 KiB (``truncated: true`` past the cap);
      * binary — a "binary file, N bytes" note, never raw bytes.
    """
    row = await deliverables.get(deliverable_id)
    if row is None or row.workspace_id != workspace_id:
        return None

    # Ref whitelist: only the deliverable's own declared artifact_refs are
    # serveable — never an arbitrary path the caller supplies.
    payload = row.payload if isinstance(row.payload, dict) else {}
    if ref not in artifact_refs_of(payload):
        return None

    try:
        raw = store.read_bytes(row.run_id, ref)
    except ValueError as exc:
        # Traversal / absolute ref — refused by the store's centralized guard.
        # Surface as 404 (never leak existence across the boundary).
        logger.debug("artifact_traversal_refused", run_id=str(row.run_id), ref=ref, error=str(exc))
        return None
    except FileNotFoundError:
        # W1/W2: a product-bound run's worktree is REMOVED after auto-ship merges
        # it to the product's main, so the produced file no longer lives in the
        # run dir — it lives in the product workspace main checkout. Fall back
        # there before declaring the content gone (else the Files viewer can
        # never open a shipped product run's files). Non-product runs (no main to
        # fall back to) keep the calm 404.
        fallback = await _read_from_product_main(session, row.run_id, ref)
        if fallback is None:
            return None
        raw = fallback
    except IsADirectoryError:
        # ``ref`` resolves to a directory inside the run dir (e.g. ``src/``).
        # Calm 404 — not a file, no content to serve.
        return None
    if _looks_binary(raw):
        return ArtifactContentResponse(
            ref=ref,
            content=f"Binary file, {len(raw)} bytes — not shown.",
            truncated=False,
            binary=True,
        )

    truncated = len(raw) > MAX_CONTENT_BYTES
    text = raw[:MAX_CONTENT_BYTES].decode("utf-8", errors="replace")
    return ArtifactContentResponse(
        ref=ref,
        content=text,
        truncated=truncated,
        binary=False,
    )


__all__ = ["read_deliverable_artifact", "run_artifact_store"]
