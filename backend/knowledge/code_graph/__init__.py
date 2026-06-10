"""Lift E20 — deterministic AST → graph pipeline for the bootstrap path.

The old bootstrap fed every source file to the LLM individually. On the
bsvibe-app dogfood that produced 12,199 notes (4,398 of them empty
``auto_stub`` entities) — a vault the founder could not navigate.

E20 replaces that with a Graphify-inspired pipeline:

1. :mod:`backend.knowledge.code_graph.parser` extracts language-aware AST
   nodes (modules, classes, functions, methods, doc sections) from
   Python / TypeScript / JavaScript / Markdown using ``tree-sitter``
   grammars. No LLM call on code.
2. :mod:`backend.knowledge.code_graph.graph` builds a ``networkx.DiGraph``
   from those nodes + the imports/calls/inherits/doc-references edges.
3. :mod:`backend.knowledge.code_graph.community` runs Leiden community
   detection on the undirected projection so each community can be
   summarized by ONE LLM call (vs one call per file).
4. The graph is persisted to the vault at
   ``<vault_root>/<region>/<workspace_id>/code_graph/graph.json`` and
   queried back via the ``bsvibe_graph_*`` MCP tools (D phase).

The whole pipeline is deterministic — same repo + same parser version
= same graph — so the founder's dogfood is reproducible across runs.
"""

from __future__ import annotations

from backend.knowledge.code_graph.types import (
    CodeEdge,
    CodeNode,
    EdgeKind,
    NodeKind,
)

__all__ = [
    "CodeEdge",
    "CodeNode",
    "EdgeKind",
    "NodeKind",
]
