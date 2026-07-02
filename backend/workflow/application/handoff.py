"""Design→impl handoff context (P1-L2b).

Reads the design run's produced spec artifact(s) so the IMPLEMENTATION run can
fold them into its work context. The impl run carries ``design_run_id`` +
``design_artifact_refs`` on its payload (set by the AgentRunner chaining in
P1-L2a). The files live either in the product's ``main`` checkout (a
product-bound design run that auto-shipped — its per-run worktree is gone) or,
for a non-product / un-shipped design run, in the design run's own workspace
dir. We try the product main first, then fall back to the run dir; both reads
go through the centralized traversal guard.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog

from backend.storage.artifact_store import LocalFilesystemArtifactStore

if TYPE_CHECKING:
    from pathlib import Path

    from backend.config import Settings
    from backend.workflow.infrastructure.db import ExecutionRun

logger = structlog.get_logger(__name__)

#: Per-spec read cap — a design spec is small; this guards the work prompt from
#: an accidental large blob blowing the local model's generation budget.
_MAX_SPEC_BYTES = 32 * 1024
#: Cap the number of design artifacts folded in (the rest are referenced only).
_MAX_SPECS = 5


def _read_one(*, root: Path, key: uuid.UUID, ref: str) -> bytes | None:
    """Read ``ref`` from ``<root>/<key>/`` via the guarded store, or ``None``."""
    store = LocalFilesystemArtifactStore(root)
    try:
        return store.read_bytes(key, ref)
    except (ValueError, FileNotFoundError, IsADirectoryError):
        return None


_SPEC_PREAMBLE = "The prior DESIGN stage produced this specification — implement it:\n\n"


def _read_spec_sections(
    *, product_id: uuid.UUID | None, design_run_id: uuid.UUID, refs: list[str], settings: Settings
) -> list[str]:
    """Read the design spec ``refs`` from the product main (shipped) or the
    design run's own dir, skipping unreadable / binary artifacts."""
    from pathlib import Path  # noqa: PLC0415

    product_root = Path(settings.product_workspace_root)
    run_root = Path(settings.run_workspace_root)
    sections: list[str] = []
    for ref in [r for r in refs if isinstance(r, str)][:_MAX_SPECS]:
        raw: bytes | None = None
        # Product main first (the shipped location), then the design run's dir.
        if product_id is not None:
            raw = _read_one(root=product_root, key=product_id, ref=ref)
        if raw is None:
            raw = _read_one(root=run_root, key=design_run_id, ref=ref)
        if raw is None:
            logger.info("design_spec_unreadable", design_run_id=str(design_run_id), ref=ref)
            continue
        # Skip binary artifacts (e.g. a ``.pyc`` the design stage produced by
        # running its tests). Their NUL bytes are valid UTF-8 but ILLEGAL in a
        # Postgres text column, so folding one into the impl prompt crashes the
        # executor-task write. A NUL byte is the reliable binary signal.
        if b"\x00" in raw:
            logger.info("design_spec_skipped_binary", design_run_id=str(design_run_id), ref=ref)
            continue
        text = raw[:_MAX_SPEC_BYTES].decode("utf-8", errors="replace")
        sections.append(f"### {ref}\n{text}")
    return sections


def capture_design_spec_text(
    *, product_id: uuid.UUID | None, design_run_id: uuid.UUID, refs: list[str], settings: Settings
) -> str | None:
    """Read + join the design spec NOW (at impl-run spawn time, while the design
    worktree still exists) so the text can be inlined on the impl run's payload.

    This is the durable half of the handoff: reading at DISPATCH time (later)
    raced worktree cleanup and a held (un-shipped) design run whose spec never
    reached product main → ``has_spec=false`` (findings 2026-07-01). Capturing
    the text at spawn — when the design worktree is guaranteed present — removes
    that dependency. ``None`` when nothing readable (the impl run still proceeds
    on an honest partial)."""
    if not refs:
        return None
    sections = _read_spec_sections(
        product_id=product_id, design_run_id=design_run_id, refs=refs, settings=settings
    )
    return _SPEC_PREAMBLE + "\n\n".join(sections) if sections else None


def read_design_context(run: ExecutionRun, settings: Settings) -> str | None:
    """The design spec text to seed the impl run's context, or ``None``.

    Prefers the spec text INLINED on the payload at spawn (``design_spec_text``,
    see :func:`capture_design_spec_text`) — durable across worktree cleanup /
    hold. Falls back to reading the ``design_artifact_refs`` from disk for older
    runs seeded before inlining. ``None`` when the run isn't an impl stage or no
    spec content is available (best-effort: a missing file is skipped, not
    fatal)."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    inlined = payload.get("design_spec_text")
    if isinstance(inlined, str) and inlined.strip():
        return inlined

    design_run_id_raw = payload.get("design_run_id")
    refs = payload.get("design_artifact_refs")
    if not isinstance(design_run_id_raw, str) or not isinstance(refs, list) or not refs:
        return None
    try:
        design_run_id = uuid.UUID(design_run_id_raw)
    except ValueError:
        return None

    sections = _read_spec_sections(
        product_id=run.product_id, design_run_id=design_run_id, refs=refs, settings=settings
    )
    return _SPEC_PREAMBLE + "\n\n".join(sections) if sections else None


__all__ = ["capture_design_spec_text", "read_design_context"]
