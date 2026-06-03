"""Repo bootstrap pipeline (Lift A v2).

Founder creates a Product with ``repo_url`` → background job clones it +
hands a small set of deterministic artifacts (file tree, manifests, top-level
docs) plus every source file as a raw artifact to
:class:`backend.knowledge.facade.Knowledge.ingest`. The LLM-side compiler
(``IngestCompiler.compile_batch``) classifies + chunks; the resulting concept
nodes + edges appear in ``KnowledgeGraphView`` with no per-language extractor
involved.

Public surface:

* :func:`run_repo_bootstrap` — the orchestrator coroutine wired by the
  runtime layer. Takes the cloned repo path + workspace + region, returns
  an :class:`IngestResult`.
* :class:`BootstrapTooLargeError` — raised when whole-repo caps are
  exceeded; surfaces as ``bootstrap_status="failed:too_large"``.
* :class:`BootstrapRepository` — Protocol the runtime layer uses to flip
  the per-Product status column without coupling the application layer to
  SQLAlchemy.
"""

from __future__ import annotations

from backend.products.application.bootstrap.anchor_backfill import (
    register_bootstrap_anchors,
)
from backend.products.application.bootstrap.orchestrator import (
    BootstrapTooLargeError,
    run_repo_bootstrap,
)
from backend.products.application.bootstrap.repository import (
    BootstrapProgress,
    BootstrapRepository,
)

__all__ = [
    "BootstrapProgress",
    "BootstrapRepository",
    "BootstrapTooLargeError",
    "register_bootstrap_anchors",
    "run_repo_bootstrap",
]
