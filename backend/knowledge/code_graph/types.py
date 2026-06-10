"""Code graph value types — Lift E20.

The :class:`CodeNode` and :class:`CodeEdge` records are the deterministic
contract between the AST extractors (:mod:`backend.knowledge.code_graph.parser`)
and the graph builder + LLM summarizer. They survive JSON round-trip via
the dict helpers below so the persisted ``graph.json`` is human-readable
and the MCP query surface can re-hydrate without pulling in tree-sitter.

Design invariants:

* Node IDs are workspace-stable: ``<lang>:<rel_posix_path>::<qualname>``
  where ``qualname`` is dotted (``ClassName.method_name`` for methods,
  ``module`` for the module itself, the heading text for a markdown
  ``doc_section``). Same path + same code = same ID across runs.
* ``CodeNode`` is hashable (frozen dataclass) so the graph builder can
  use it as a NetworkX node key without auxiliary id maps when needed.
* Lines are 1-based (editor convention) — the parser converts from
  tree-sitter's 0-based ``Point`` at the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeKind(StrEnum):
    """The five node kinds the AST extractor can emit.

    ``module`` — a source file in any supported language.
    ``function`` — a free function (Python ``def`` at module scope, JS
    function declaration, etc.).
    ``class`` — a class definition.
    ``method`` — a function defined inside a class (or in TS, on an
    object literal). Carries the parent class via ``parent_id``.
    ``doc_section`` — a heading-led section in a Markdown file.
    """

    MODULE = "module"
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    DOC_SECTION = "doc_section"


class EdgeKind(StrEnum):
    """The four edge kinds the AST extractor can emit.

    ``imports`` — module ``A`` imports module ``B`` (or a symbol from it).
    ``calls`` — function/method ``A`` invokes function/method ``B`` where
    ``B`` is resolvable within the parsed node set. External calls are
    dropped (we have no module path for them).
    ``inherits`` — class ``A`` inherits from class ``B``.
    ``doc_references`` — a markdown wiki-link ``[[X]]`` mentions another
    node whose name matches ``X``.
    """

    IMPORTS = "imports"
    CALLS = "calls"
    INHERITS = "inherits"
    DOC_REFERENCES = "doc_references"


@dataclass(frozen=True, slots=True)
class CodeNode:
    """One node in the code graph.

    Frozen so it can be used as a NetworkX hashable node id; slots-only
    so memory cost on a 10k-node graph stays bounded.
    """

    id: str
    kind: NodeKind
    name: str
    path: str
    start_line: int
    end_line: int
    signature: str | None = None
    docstring: str | None = None
    parent_id: str | None = None
    language: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape preserved across persisted graphs."""
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "parent_id": self.parent_id,
            "language": self.language,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeNode:
        return cls(
            id=str(raw["id"]),
            kind=NodeKind(raw["kind"]),
            name=str(raw["name"]),
            path=str(raw["path"]),
            start_line=int(raw["start_line"]),
            end_line=int(raw["end_line"]),
            signature=raw.get("signature"),
            docstring=raw.get("docstring"),
            parent_id=raw.get("parent_id"),
            language=str(raw.get("language", "")),
        )


@dataclass(frozen=True, slots=True)
class CodeEdge:
    """One edge between two :class:`CodeNode` ids."""

    src_id: str
    dst_id: str
    kind: EdgeKind

    def to_dict(self) -> dict[str, Any]:
        return {
            "src_id": self.src_id,
            "dst_id": self.dst_id,
            "kind": self.kind.value,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> CodeEdge:
        return cls(
            src_id=str(raw["src_id"]),
            dst_id=str(raw["dst_id"]),
            kind=EdgeKind(raw["kind"]),
        )


@dataclass
class ParseResult:
    """All nodes + edges extracted from one file."""

    nodes: list[CodeNode] = field(default_factory=list)
    edges: list[CodeEdge] = field(default_factory=list)


__all__ = [
    "CodeEdge",
    "CodeNode",
    "EdgeKind",
    "NodeKind",
    "ParseResult",
]
