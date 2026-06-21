"""End-to-end pipeline: walk → filter → parse → graph → community → artifacts.

Lift E20 Phase C — the new bootstrap path. Replaces the old
"feed every source file to the LLM" loop with:

1. Walk the repo (existing :func:`walk_repo`).
2. Apply :class:`BootstrapFileFilter` (Phase A) — drop lockfiles /
   vendor / binaries / IDE cruft / oversized files BEFORE parsing.
3. Skip test files (path-pattern match) — they don't carry
   project-design insight and would bloat the graph.
4. Parse code files with the language-specific tree-sitter strategy
   (:mod:`backend.knowledge.code_graph.parser`).
5. Build a ``networkx.DiGraph`` from all nodes + edges
   (:mod:`backend.knowledge.code_graph.graph`).
6. Run Leiden community detection on the undirected projection
   (:mod:`backend.knowledge.code_graph.community`).
7. For each community: render a SINGLE artifact — top-K nodes by
   PageRank, with signatures + docstrings + the leading doc-section
   excerpts. The LLM gets one synthesis call per community, not per
   file.
8. For each ``markdown`` file: emit a per-doc artifact (markdown
   content is insight-dense; the founder's README / ARCHITECTURE
   should always go through the prompt separately).

The artifact dicts are the standard
``{"label", "content", "kind"}`` shape the existing
:class:`Knowledge.ingest` boundary already understands.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx
import structlog

from backend.knowledge.code_graph.community import annotate_communities
from backend.knowledge.code_graph.graph import annotate_pagerank, build_graph, save_graph
from backend.knowledge.code_graph.parser import detect_language, parse_source
from backend.knowledge.code_graph.types import NodeKind

# NB — ``BootstrapFileFilter`` + ``walk_repo`` are imported lazily inside
# :func:`build_code_graph_artifacts` to break the import cycle:
# ``products.application.bootstrap.orchestrator`` imports this module,
# and that package's ``__init__`` re-exports the orchestrator, so a
# top-level import here would load the orchestrator BEFORE this
# module's symbols are defined.
if TYPE_CHECKING:
    from backend.products.application.bootstrap.walker import WalkedFile

logger = structlog.get_logger(__name__)

#: Cap on nodes per community chunk handed to the LLM. Sized so the
#: signatures + docstrings + community-summary header fit under the
#: chunk-budget probe; founder's ``opencode`` daemons run ~32k context,
#: this leaves room for the existing related-context block.
_TOP_NODES_PER_COMMUNITY = 30

#: Cap on the rendered length of any single community chunk's content.
#: Belt-and-suspenders for a community with very long signatures.
_MAX_COMMUNITY_CHUNK_CHARS = 16_000

#: Per-doc cap; the markdown artifact gets the WHOLE file unless it's
#: huge. Matches the legacy source_collector's per-file cap.
_MAX_DOC_CHARS = 32_000


_TEST_PATH_PATTERNS = (
    re.compile(r"(^|/)tests?(/|$)"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"\.test\.[jt]sx?$"),
    re.compile(r"\.spec\.[jt]sx?$"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.go$"),
)


def is_test_path(rel_path: str) -> bool:
    """``True`` when ``rel_path`` is a test file by language convention.

    The check is intentionally generous — false positives (e.g. a
    folder literally called ``tests/`` that isn't tests) cost the
    bootstrap nothing because skipping a non-test still skips noise.
    """
    return any(p.search(rel_path) for p in _TEST_PATH_PATTERNS)


@dataclass
class CodeGraphResult:
    """Output of :func:`build_code_graph_artifacts`."""

    graph: nx.DiGraph
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    filter_summary: dict[str, int] = field(default_factory=dict)
    walked_count: int = 0
    parsed_count: int = 0
    community_count: int = 0


def build_code_graph_artifacts(repo_root: Path) -> CodeGraphResult:
    """Walk → parse → graph → community → render artifacts.

    Pure function; reads from ``repo_root``, emits a :class:`CodeGraphResult`.
    The orchestrator persists the graph to the vault and hands the
    artifacts to ``Knowledge.ingest``.
    """
    # Lazy imports break the import cycle with the bootstrap package.
    from backend.products.application.bootstrap.bootstrap_filter import (  # noqa: PLC0415
        BootstrapFileFilter,
    )
    from backend.products.application.bootstrap.walker import (  # noqa: PLC0415
        walk_repo,
    )

    file_filter = BootstrapFileFilter(repo_root=repo_root)
    walked: list[WalkedFile] = list(walk_repo(repo_root, file_filter=file_filter))

    # Test-path drop happens AFTER the filter so the per-reason
    # summary still reports the real filter counters separately. We
    # tally the dropped tests independently for the audit log.
    code_files: list[WalkedFile] = []
    test_dropped = 0
    markdown_files: list[WalkedFile] = []
    for w in walked:
        if is_test_path(w.rel_path):
            test_dropped += 1
            continue
        lang = detect_language(w.rel_path)
        if lang is None:
            continue
        if lang == "markdown":
            markdown_files.append(w)
        code_files.append(w)

    all_nodes = []
    all_edges = []
    parsed = 0
    for w in code_files:
        lang = detect_language(w.rel_path)
        if lang is None:
            continue
        try:
            source = w.abs_path.read_bytes()
        except OSError:
            continue
        result = parse_source(path=w.rel_path, source=source, language=lang)
        all_nodes.extend(result.nodes)
        all_edges.extend(result.edges)
        parsed += 1

    graph = build_graph(all_nodes, all_edges)
    annotate_communities(graph)
    # Persist centrality onto every node so the saved graph.json powers
    # MCP graph_search ranking + E25 community top_symbols. Without this
    # every node lands at pagerank 0.0 and ranking degrades to node order.
    annotate_pagerank(graph)

    community_chunks = _render_community_chunks(graph)
    doc_artifacts = _render_doc_artifacts(markdown_files)

    artifacts: list[dict[str, Any]] = []
    artifacts.extend(community_chunks)
    artifacts.extend(doc_artifacts)

    summary = file_filter.summary()
    if test_dropped:
        summary["test_path"] = test_dropped

    logger.info(
        "bootstrap_filter_summary",
        walked=len(walked),
        parsed=parsed,
        artifacts=len(artifacts),
        communities=len({graph.nodes[n].get("community_id") for n in graph.nodes}),
        **summary,
    )

    return CodeGraphResult(
        graph=graph,
        artifacts=artifacts,
        filter_summary=summary,
        walked_count=len(walked),
        parsed_count=parsed,
        community_count=len(community_chunks),
    )


def _render_community_chunks(graph: nx.DiGraph) -> list[dict[str, Any]]:
    """One artifact per community, content = top-K nodes by PageRank.

    Edges into a node raise its PageRank; that's the Aider-style
    "most-referenced symbols are the most informative summary input"
    signal we lean on.
    """
    if graph.number_of_nodes() == 0:
        return []

    # PageRank is already annotated onto every node by the pipeline
    # (annotate_pagerank). Read the cached scores here so chunk ordering
    # matches what graph_search and the labels will surface.
    scores = {n: float(graph.nodes[n].get("pagerank", 0.0)) for n in graph.nodes}

    # Group node ids by community.
    communities: dict[int, list[str]] = {}
    for nid in graph.nodes:
        cid = graph.nodes[nid].get("community_id")
        if cid is None:
            continue
        communities.setdefault(int(cid), []).append(nid)

    artifacts: list[dict[str, Any]] = []
    for cid, members in sorted(communities.items()):
        # Sort each community's members by descending PageRank.
        members.sort(key=lambda n: -scores.get(n, 0.0))
        top = members[:_TOP_NODES_PER_COMMUNITY]
        content = _render_community_content(cid, top, graph)
        if not content.strip():
            continue
        artifacts.append(
            {
                "label": f"code-graph/community-{cid}",
                "content": content[:_MAX_COMMUNITY_CHUNK_CHARS],
                "kind": "code-graph-community",
            }
        )
    return artifacts


def _render_community_content(cid: int, node_ids: Iterable[str], graph: nx.DiGraph) -> str:
    """Render the LLM-facing summary of one community's top nodes.

    Headers + signatures + docstrings + neighbor counts. The output is
    plain markdown so the prompt sees a structured chunk it can mine
    Pattern/Principle/TechInsight notes from.
    """
    lines: list[str] = [f"# Community {cid}", ""]
    # Tally per-language presence for a quick "what's in here" header.
    langs: dict[str, int] = {}
    paths: set[str] = set()
    for nid in node_ids:
        attrs = graph.nodes[nid]
        lang = attrs.get("language", "")
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
        path = attrs.get("path", "")
        if path:
            paths.add(path)
    if langs:
        lang_summary = ", ".join(f"{name}: {n}" for name, n in sorted(langs.items()))
        lines.append(f"_Languages: {lang_summary}_")
    if paths:
        lines.append(f"_Files: {len(paths)}_")
    lines.append("")

    for nid in node_ids:
        attrs = graph.nodes[nid]
        kind = attrs.get("kind", "")
        name = attrs.get("name", nid)
        path = attrs.get("path", "")
        line = attrs.get("start_line", 0)
        if kind == NodeKind.MODULE.value:
            lines.append(f"## Module `{name}` — {path}")
        elif kind == NodeKind.CLASS.value:
            lines.append(f"## Class `{name}` — {path}:{line}")
        elif kind in (NodeKind.FUNCTION.value, NodeKind.METHOD.value):
            lines.append(f"## {kind.title()} `{name}` — {path}:{line}")
        elif kind == NodeKind.DOC_SECTION.value:
            lines.append(f"## Doc section `{name}` — {path}:{line}")
        else:
            continue
        signature = attrs.get("signature")
        if signature:
            lines.append(f"```\n{signature}\n```")
        docstring = attrs.get("docstring")
        if docstring:
            lines.append(docstring)
        lines.append("")
    return "\n".join(lines)


def _render_doc_artifacts(docs: list[WalkedFile]) -> list[dict[str, Any]]:
    """One artifact per markdown file — full content, capped."""
    out: list[dict[str, Any]] = []
    for w in docs:
        try:
            text = w.abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > _MAX_DOC_CHARS:
            text = text[:_MAX_DOC_CHARS] + "\n\n…[truncated for ingest seed cap]…"
        out.append(
            {
                "label": w.rel_path,
                "content": f"# File: {w.rel_path}\n\n{text}",
                "kind": "markdown-doc",
            }
        )
    return out


def persist_graph(graph: nx.DiGraph, path: Path) -> None:
    """Atomic-write the graph JSON to ``path``.

    Thin wrapper so the orchestrator only imports one module from
    ``code_graph`` for both build + persist.
    """
    save_graph(graph, path)


def persist_community_labels(graph: nx.DiGraph, path: Path) -> int:
    """Lift E25 — derive + atomically persist community labels.

    Returns the number of communities that received a label so the
    orchestrator can audit-log the count. Soft-fails on OS errors via the
    caller's existing try/except — same pattern as ``persist_graph``.
    """
    from backend.knowledge.code_graph.community import (  # noqa: PLC0415
        derive_community_labels,
    )

    labels = derive_community_labels(graph)
    payload = {
        "version": 1,
        "communities": [labels[cid] for cid in sorted(labels)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False)
        tmp_path = tmp.name
    os.replace(tmp_path, path)
    return len(labels)


def code_graph_vault_path(*, vault_root: Path) -> Path:
    """Return the per-workspace ``code_graph/graph.json`` path.

    The vault root is the workspace-scoped ``<vault>/<region>/<id>/``
    directory the existing Knowledge facade owns.
    """
    return vault_root / "code_graph" / "graph.json"


def community_labels_vault_path(*, vault_root: Path) -> Path:
    """Lift E25 — companion path for ``code_graph/communities.json``."""
    return vault_root / "code_graph" / "communities.json"


__all__ = [
    "CodeGraphResult",
    "build_code_graph_artifacts",
    "code_graph_vault_path",
    "community_labels_vault_path",
    "is_test_path",
    "persist_community_labels",
    "persist_graph",
]
