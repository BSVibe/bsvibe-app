"""스텝 간 인수인계 — 앞 스텝의 산출물을 다음 스텝의 컨텍스트로.

Reads the PRIOR step's produced artifact(s) so the next step's run can fold
them into its work context. The next run carries ``prior_run_id`` +
``prior_artifact_refs`` on its payload (set by the AgentRunner chaining). The
files live either in the product's ``main`` checkout (a product-bound run that
auto-shipped — its per-run worktree is gone) or, for a non-product / un-shipped
run, in that run's own workspace dir. We try the product main first, then fall
back to the run dir; both reads go through the centralized traversal guard.

This used to be design→impl specific (``design_run_id`` / ``design_spec_text``,
with a preamble that told the reader to "implement this specification"). The
steps are now named by the founder's own routing vocabulary, so neither end of
the handoff can assume what the prior step produced or what this one should do
with it — the step's own intent says that.
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


#: Names WHERE the text came from and nothing more. It deliberately does not
#: tell the reader what to do with it: the step's own ``intent`` is its
#: directive, and a preamble that says "implement this" would contradict a step
#: the founder named something else.
_PRIOR_OUTPUT_PREAMBLE = "The prior step of this work produced the following:\n\n"


def _read_sections(
    *, product_id: uuid.UUID | None, prior_run_id: uuid.UUID, refs: list[str], settings: Settings
) -> list[str]:
    """Read the prior step's ``refs`` from the product main (shipped) or that
    run's own dir, skipping unreadable / binary artifacts."""
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
            raw = _read_one(root=run_root, key=prior_run_id, ref=ref)
        if raw is None:
            logger.info("prior_step_output_unreadable", prior_run_id=str(prior_run_id), ref=ref)
            continue
        # Skip binary artifacts (e.g. a ``.pyc`` a step produced by running its
        # tests). Their NUL bytes are valid UTF-8 but ILLEGAL in a Postgres text
        # column, so folding one into the next step's prompt crashes the
        # executor-task write. A NUL byte is the reliable binary signal.
        if b"\x00" in raw:
            logger.info("prior_step_output_binary", prior_run_id=str(prior_run_id), ref=ref)
            continue
        text = raw[:_MAX_SPEC_BYTES].decode("utf-8", errors="replace")
        sections.append(f"### {ref}\n{text}")
    return sections


def capture_prior_step_output(
    *, product_id: uuid.UUID | None, prior_run_id: uuid.UUID, refs: list[str], settings: Settings
) -> str | None:
    """Read + join the prior step's output NOW (at spawn time, while its
    worktree still exists) so the text can be inlined on the next run's payload.

    This is the durable half of the handoff: reading at DISPATCH time (later)
    raced worktree cleanup and a held (un-shipped) run whose output never
    reached product main → ``has_spec=false`` (findings 2026-07-01). Capturing
    at spawn — when the worktree is guaranteed present — removes that
    dependency. ``None`` when nothing readable (the next step still proceeds on
    an honest partial)."""
    if not refs:
        return None
    sections = _read_sections(
        product_id=product_id, prior_run_id=prior_run_id, refs=refs, settings=settings
    )
    return _PRIOR_OUTPUT_PREAMBLE + "\n\n".join(sections) if sections else None


def read_prior_step_context(run: ExecutionRun, settings: Settings) -> str | None:
    """The prior step's output text to seed this run's context, or ``None``.

    Prefers the text INLINED on the payload at spawn (``prior_output_text``, see
    :func:`capture_prior_step_output`) — durable across worktree cleanup / hold.
    Falls back to reading ``prior_artifact_refs`` from disk. ``None`` when this
    run is not a later step or no content is available (best-effort: a missing
    file is skipped, not fatal).

    The legacy ``design_*`` keys are still read so a run that was already
    mid-chain when this shipped finishes with its context intact — prod carries
    11 such payloads (all terminal, but a deploy must not be able to blind a
    live one)."""
    payload = run.payload if isinstance(run.payload, dict) else {}
    inlined = payload.get("prior_output_text") or payload.get("design_spec_text")
    if isinstance(inlined, str) and inlined.strip():
        return inlined

    prior_run_id_raw = payload.get("prior_run_id") or payload.get("design_run_id")
    refs = payload.get("prior_artifact_refs") or payload.get("design_artifact_refs")
    if not isinstance(prior_run_id_raw, str) or not isinstance(refs, list) or not refs:
        return None
    try:
        prior_run_id = uuid.UUID(prior_run_id_raw)
    except ValueError:
        return None

    sections = _read_sections(
        product_id=run.product_id, prior_run_id=prior_run_id, refs=refs, settings=settings
    )
    return _PRIOR_OUTPUT_PREAMBLE + "\n\n".join(sections) if sections else None


__all__ = ["capture_prior_step_output", "read_prior_step_context"]
