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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from backend.knowledge.code_graph.pipeline import (
    build_code_graph_artifacts,
    code_graph_vault_path,
    persist_graph,
)
from backend.knowledge.facade import IngestRequest, IngestResult, Knowledge
from backend.products.application.bootstrap.extractors import file_tree, manifests
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
    Knowledge-facade-returned proposals/notes counts for the audit log,
    plus (Lift E8 Bug 2) the compile-time failure signal the runtime uses
    to decide ``failed:ingest`` vs ``complete``.
    """

    artifacts_count: int
    ingest_result: IngestResult

    @property
    def notes_written(self) -> int:
        """How many notes were created OR updated by the ingest. Bug 2 signal."""
        return self.ingest_result.notes_created + self.ingest_result.notes_updated

    @property
    def chunk_failures(self) -> int:
        """How many compile chunks raised. Bug 2 signal."""
        return self.ingest_result.chunk_failures


async def run_repo_bootstrap(
    *,
    repo_root: Path,
    workspace_id: uuid.UUID,
    region: str,
    knowledge: Knowledge,
    vault_root: Path | None = None,
) -> BootstrapOutcome:
    """Run the full bootstrap pipeline against an already-cloned repo.

    Lift E20 — the pipeline is now Graphify-inspired:

    1. The code-graph pipeline (:mod:`backend.knowledge.code_graph.pipeline`)
       walks + filters + parses + builds the directed graph + runs
       Leiden community detection. Each community becomes ONE LLM
       artifact (top-K nodes' signatures + docstrings) instead of one
       artifact per source file.
    2. Markdown files (READMEs, ``.bsvibe/*.md``) still get a per-file
       artifact — they're insight-dense and worth a dedicated prompt.
    3. The file-tree + manifest extractors still run on the raw walked
       set so the LLM sees the deterministic "what's this project's
       shape" structural seeds first.
    4. When ``vault_root`` is supplied, the graph is persisted to
       ``<vault_root>/code_graph/graph.json`` so the MCP graph query
       tools (Phase D) can serve it back later.

    Caller responsibilities (the runtime layer):

    * Set the workspace's per-row ``bootstrap_status`` BEFORE this call
      ("analyzing") and AFTER ("complete" / "failed:…"). This function
      does not touch the DB; it deals with files + the Knowledge facade.
    * Provide a ``Knowledge`` instance bound to the same workspace + region.

    Returns a :class:`BootstrapOutcome` on success. Raises
    :class:`BootstrapTooLargeError` when whole-repo caps are exceeded
    (caller writes ``failed:too_large``). Other errors propagate.
    """
    # Lift E20 — run the code-graph pipeline (filter + parse + graph +
    # communities) once. It also drives the per-doc markdown artifact
    # rendering, so the orchestrator's own ``walked`` list now only
    # supplies the file-tree / manifests structural seeds.
    try:
        code_graph_result = build_code_graph_artifacts(repo_root)
    except BootstrapTooLargeError:
        logger.warning(
            "product_bootstrap_too_large",
            workspace_id=str(workspace_id),
            region=region,
        )
        raise

    # Persist the graph so the MCP query surface can serve it back.
    if vault_root is not None:
        try:
            persist_graph(
                code_graph_result.graph,
                code_graph_vault_path(vault_root=vault_root),
            )
        except OSError:
            logger.warning(
                "product_bootstrap_graph_persist_failed",
                workspace_id=str(workspace_id),
                region=region,
                exc_info=True,
            )
        # Lift E25 — derive + persist community labels so the MCP
        # ``bsvibe_graph_community`` tool can answer "why are these grouped"
        # with a deterministic label (common path prefix), top symbols, and
        # file count. Soft-fails: the graph itself is still queryable even
        # if the label sidecar can't be written.
        try:
            from backend.knowledge.code_graph.pipeline import (  # noqa: PLC0415
                community_labels_vault_path,
                persist_community_labels,
            )

            labeled = persist_community_labels(
                code_graph_result.graph,
                community_labels_vault_path(vault_root=vault_root),
            )
            logger.info(
                "product_bootstrap_community_labels_persisted",
                workspace_id=str(workspace_id),
                region=region,
                labeled=labeled,
            )
        except OSError:
            logger.warning(
                "product_bootstrap_community_labels_persist_failed",
                workspace_id=str(workspace_id),
                region=region,
                exc_info=True,
            )

    # Re-walk the repo with the same filter so we can build the
    # file-tree + manifests structural seeds. (The code-graph pipeline
    # already filters; we re-walk lightly here to feed the structural
    # extractors that consume :class:`WalkedFile`.)
    structural_walked: list[WalkedFile] = []
    try:
        from backend.products.application.bootstrap.bootstrap_filter import (  # noqa: PLC0415
            BootstrapFileFilter,
        )

        for w in walk_repo(repo_root, file_filter=BootstrapFileFilter(repo_root=repo_root)):
            structural_walked.append(w)
    except BootstrapTooLargeError:
        logger.warning(
            "product_bootstrap_too_large_structural",
            workspace_id=str(workspace_id),
            region=region,
            walked_so_far=len(structural_walked),
        )
        raise

    artifacts: list[dict[str, Any]] = []
    artifacts.append(file_tree(structural_walked))
    artifacts.extend(manifests(structural_walked))
    artifacts.extend(code_graph_result.artifacts)

    logger.info(
        "product_bootstrap_artifacts_built",
        workspace_id=str(workspace_id),
        region=region,
        walked=code_graph_result.walked_count,
        parsed=code_graph_result.parsed_count,
        communities=code_graph_result.community_count,
        artifacts=len(artifacts),
        filter_summary=code_graph_result.filter_summary,
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


__all__ = [
    "BootstrapOutcome",
    "BootstrapTooLargeError",
    "run_repo_bootstrap",
]
