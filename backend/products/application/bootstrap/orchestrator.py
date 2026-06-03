"""Repo bootstrap orchestrator.

Lift A v2 — the entrypoint the runtime layer drives. Composes the four
stages — walker, selector (via extractors/source_collector), extractors,
source_collector — into one coroutine that hands an
:class:`~backend.knowledge.facade.IngestRequest` to ``Knowledge.ingest``.

This is the layer that knows the WHOLE artifact set; everything below it
deals with one bucket at a time. Lifecycle:

1. ``mark_status(pending → cloning)`` — at runtime boundary (caller).
2. Repo already cloned to ``repo_root`` (caller's job).
3. ``mark_status(analyzing)`` — walker + extractors + source collector
   build the artifact list.
4. ``mark_status(ingesting)`` — hand to ``Knowledge.ingest``.
5. ``mark_status(complete, artifacts_count=N)`` — done.

A :class:`BootstrapTooLargeError` raised by the walker is caught and
re-raised so the caller can write ``failed:too_large``; everything else
propagates so the runtime layer's outer try/except writes a generic
``failed:ingest``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from backend.knowledge.facade import IngestRequest, IngestResult, Knowledge
from backend.products.application.bootstrap.extractors import docs, file_tree, manifests
from backend.products.application.bootstrap.source_collector import collect_source_artifacts
from backend.products.application.bootstrap.walker import (
    BootstrapTooLargeError,
    WalkedFile,
    walk_repo,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BootstrapOutcome:
    """Result the runtime layer needs to write back to the row.

    ``artifacts_count`` is the count handed to ``Knowledge.ingest`` (NOT
    the count IngestCompiler then chunked). ``ingest_result`` carries the
    Knowledge-facade-returned proposals/notes counts for the audit log.
    """

    artifacts_count: int
    ingest_result: IngestResult


async def run_repo_bootstrap(
    *,
    repo_root: Path,
    workspace_id: uuid.UUID,
    region: str,
    knowledge: Knowledge,
) -> BootstrapOutcome:
    """Run the full bootstrap pipeline against an already-cloned repo.

    Caller responsibilities (the runtime layer):

    * Set the workspace's per-row ``bootstrap_status`` BEFORE this call
      ("analyzing") and AFTER ("complete" / "failed:…"). This function
      does not touch the DB; it deals with files + the Knowledge facade.
    * Provide a ``Knowledge`` instance bound to the same workspace + region.

    Returns a :class:`BootstrapOutcome` on success. Raises
    :class:`BootstrapTooLargeError` when whole-repo caps are exceeded
    (caller writes ``failed:too_large``). Other errors propagate.
    """
    walked: list[WalkedFile] = []
    try:
        for w in walk_repo(repo_root):
            walked.append(w)
    except BootstrapTooLargeError:
        logger.warning(
            "product_bootstrap_too_large",
            workspace_id=str(workspace_id),
            region=region,
            walked_so_far=len(walked),
        )
        raise

    artifacts: list[dict[str, Any]] = _build_artifacts(walked)
    logger.info(
        "product_bootstrap_artifacts_built",
        workspace_id=str(workspace_id),
        region=region,
        walked=len(walked),
        artifacts=len(artifacts),
    )

    if not artifacts:
        # An empty repo (or one whose every file got filtered) still gets
        # the file-tree seed — which says "no files" — so the LLM has at
        # least one artifact. Defensive: never call ingest with an empty
        # list (the compiler treats that as a no-op anyway, but the
        # explicit log keeps the audit trail clean).
        result = await knowledge.ingest(
            IngestRequest(
                workspace_id=workspace_id,
                region=region,
                artifacts=[
                    {
                        "label": "repo/empty.md",
                        "content": "# Repository\n\n(no walked files — repo is empty)",
                        "kind": "file-tree",
                    }
                ],
            )
        )
        return BootstrapOutcome(artifacts_count=1, ingest_result=result)

    result = await knowledge.ingest(
        IngestRequest(
            workspace_id=workspace_id,
            region=region,
            artifacts=artifacts,
        )
    )
    return BootstrapOutcome(artifacts_count=len(artifacts), ingest_result=result)


def _build_artifacts(walked: Sequence[WalkedFile]) -> list[dict[str, Any]]:
    """Compose the four buckets into one ordered artifact list.

    Order is fixed for stability across runs: ``[file-tree, manifests,
    docs, sources]`` — the IngestCompiler's batch chunker preserves
    input order within each chunk so the LLM sees high-signal seeds
    first.
    """
    artifacts: list[dict[str, Any]] = []
    artifacts.append(file_tree(walked))
    artifacts.extend(manifests(walked))
    artifacts.extend(docs(walked))
    artifacts.extend(collect_source_artifacts(walked))
    return artifacts


__all__ = [
    "BootstrapOutcome",
    "BootstrapTooLargeError",
    "run_repo_bootstrap",
]
