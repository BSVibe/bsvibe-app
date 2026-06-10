"""Tree-sitter AST extractor — Lift E20 Phase B.

Given a file path + source bytes + language label, returns the typed
:class:`CodeNode` + :class:`CodeEdge` records the graph builder
consumes. No LLM call. No regex on the raw source.

Languages supported in E20: Python, TypeScript, JavaScript, Markdown.
The set is deliberately small — the founder's host (bsvibe-app) is
~95% these four. New languages slot in by adding an
:class:`_LanguageStrategy` subclass + an entry in
:data:`_LANGUAGE_STRATEGIES` and exposing the grammar via
``tree_sitter_language_pack.get_language``.

Determinism:
* ``CodeNode.id`` is stable across runs for the same path + qualname.
* Children are visited in source order (tree-sitter walks left-to-right)
  so the edges list also comes out in source order.

Robustness:
* A syntax error never raises — tree-sitter parses partially and we
  surface whatever named nodes did appear, with at minimum the module
  node so downstream code can branch on "we have one file" semantics.
* Parsing happens once per file; the grammar's :class:`tree_sitter.Parser`
  is cached per process via :func:`_get_parser` so a 1k-file repo
  doesn't pay 1k grammar-loads.
"""

from __future__ import annotations

import re
import threading
from abc import ABC, abstractmethod
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import structlog

from backend.knowledge.code_graph.types import (
    CodeEdge,
    CodeNode,
    EdgeKind,
    NodeKind,
    ParseResult,
)

if TYPE_CHECKING:
    import tree_sitter

logger = structlog.get_logger(__name__)


#: The four languages E20 supports. ``tsx`` is treated as its own
#: grammar — TypeScript JSX is a superset of the TS grammar but the
#: tree-sitter grammars are distinct.
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {"python", "typescript", "tsx", "javascript", "markdown"}
)

#: Path suffix → language label. Lowercase suffix lookup.
_SUFFIX_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyx": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
}


def detect_language(path: str) -> str | None:
    """Return the language label for ``path`` or ``None`` if unsupported."""
    suffix = PurePosixPath(path).suffix.lower()
    return _SUFFIX_LANGUAGE.get(suffix)


# ---------------------------------------------------------------------------
# Parser cache — per-process. tree-sitter Parsers are cheap to call but
# carry a non-trivial grammar binding cost; we keep one per language.
# ---------------------------------------------------------------------------
_parser_cache: dict[str, tree_sitter.Parser] = {}
_parser_lock = threading.Lock()


def _get_parser(language: str) -> tree_sitter.Parser:
    """Return a cached tree-sitter parser for ``language``.

    The language pack ships per-language grammars as compiled wheels;
    we wrap each in the standard :class:`tree_sitter.Parser` so the
    Node API (``node.type``, ``node.children``, ``node.text``) is the
    canonical one and not the pack's internal binding.
    """
    cached = _parser_cache.get(language)
    if cached is not None:
        return cached
    with _parser_lock:
        cached = _parser_cache.get(language)
        if cached is not None:
            return cached
        import tree_sitter  # noqa: PLC0415 — lazy so test infra without tree-sitter still imports
        from tree_sitter_language_pack import get_language  # noqa: PLC0415

        lang = get_language(language)
        parser = tree_sitter.Parser(lang)
        _parser_cache[language] = parser
        return parser


# ---------------------------------------------------------------------------
# Per-language strategies — each picks the AST node kinds out of the
# language's grammar and emits CodeNode/CodeEdge records. The base class
# owns the shared boilerplate (module node creation, id format,
# byte-range → 1-based line conversion).
# ---------------------------------------------------------------------------
def _id_for(language: str, path: str, qualname: str) -> str:
    return f"{language}:{path}::{qualname}"


def _node_lines(node: tree_sitter.Node) -> tuple[int, int]:
    # tree-sitter Point is (row, column) 0-based; we surface 1-based.
    return node.start_point[0] + 1, node.end_point[0] + 1


def _slice_text(source: bytes, node: tree_sitter.Node) -> str:
    try:
        return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        return ""


class _LanguageStrategy(ABC):
    """Per-language extraction strategy.

    The strategy walks the tree-sitter AST once and appends nodes / edges
    to a shared :class:`ParseResult`. Languages override
    :meth:`emit` with their concrete grammar-specific logic.
    """

    language: str

    def __init__(self, language: str) -> None:
        self.language = language

    @abstractmethod
    def emit(self, path: str, source: bytes, root: tree_sitter.Node, out: ParseResult) -> None: ...


class _PythonStrategy(_LanguageStrategy):
    """Python AST extraction.

    Module ``A`` → module node. Top-level ``def`` → function. ``class``
    → class node, with nested ``def`` → method whose ``parent_id`` is
    the class node id.

    Imports: ``import x`` / ``import x.y`` / ``from x import y`` all
    produce a single ``module → x`` edge (``y`` is module-level binding
    metadata we don't materialize here).

    Calls: ``foo()`` inside any function/method body where ``foo`` is
    defined elsewhere in this same file becomes a CALLS edge from the
    enclosing function/method node. We use a same-file symbol table
    populated in the first pass and queried during the call extraction
    pass; cross-file calls are dropped (the cross-file resolver lives
    in the graph builder, not the parser).

    Inherits: ``class A(B)`` → INHERITS edge from class A to a class
    node whose unqualified name is ``B`` somewhere in the parsed file.
    Same-file resolution only.
    """

    def emit(self, path: str, source: bytes, root: tree_sitter.Node, out: ParseResult) -> None:
        module_id = _id_for(self.language, path, "module")
        # First pass — collect class+function nodes so we have a same-file
        # symbol table for the second pass's call/inherit resolution.
        symbols: dict[str, str] = {}

        def _collect(node: tree_sitter.Node, parent_qual: str | None) -> None:
            for child in node.named_children:
                if child.type == "function_definition":
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _slice_text(source, name_node)
                    qual = name if parent_qual is None else f"{parent_qual}.{name}"
                    nid = _id_for(self.language, path, qual)
                    kind = NodeKind.METHOD if parent_qual is not None else NodeKind.FUNCTION
                    parent_id = (
                        _id_for(self.language, path, parent_qual)
                        if parent_qual is not None
                        else module_id
                    )
                    start_line, end_line = _node_lines(child)
                    out.nodes.append(
                        CodeNode(
                            id=nid,
                            kind=kind,
                            name=name,
                            path=path,
                            start_line=start_line,
                            end_line=end_line,
                            signature=_extract_python_signature(source, child),
                            docstring=_extract_python_docstring(source, child),
                            parent_id=parent_id,
                            language=self.language,
                        )
                    )
                    symbols[name] = nid
                    # Recurse for nested functions/classes (rare in Py
                    # but valid). Carry parent_qual = qual so nested
                    # ``def`` becomes METHOD with the right parent.
                    body = child.child_by_field_name("body")
                    if body is not None:
                        _collect(body, qual)
                elif child.type == "class_definition":
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _slice_text(source, name_node)
                    qual = name if parent_qual is None else f"{parent_qual}.{name}"
                    nid = _id_for(self.language, path, qual)
                    parent_id = (
                        _id_for(self.language, path, parent_qual)
                        if parent_qual is not None
                        else module_id
                    )
                    start_line, end_line = _node_lines(child)
                    out.nodes.append(
                        CodeNode(
                            id=nid,
                            kind=NodeKind.CLASS,
                            name=name,
                            path=path,
                            start_line=start_line,
                            end_line=end_line,
                            signature=None,
                            docstring=_extract_python_docstring(source, child),
                            parent_id=parent_id,
                            language=self.language,
                        )
                    )
                    symbols[name] = nid
                    body = child.child_by_field_name("body")
                    if body is not None:
                        _collect(body, qual)
                else:
                    # Recurse looking for nested definitions inside other
                    # block forms (if/try/with bodies at module scope).
                    _collect(child, parent_qual)

        _collect(root, None)

        # Second pass — imports + calls + inherits using the symbol table.
        def _imports_and_calls(node: tree_sitter.Node, enclosing_id: str | None) -> None:
            for child in node.named_children:
                if child.type == "import_statement":
                    for name in _extract_python_import_names(source, child):
                        out.edges.append(
                            CodeEdge(
                                src_id=module_id,
                                dst_id=_imports_dst(name),
                                kind=EdgeKind.IMPORTS,
                            )
                        )
                elif child.type == "import_from_statement":
                    module_name_node = child.child_by_field_name("module_name")
                    module_name = _slice_text(source, module_name_node) if module_name_node else ""
                    if module_name:
                        out.edges.append(
                            CodeEdge(
                                src_id=module_id,
                                dst_id=_imports_dst(module_name),
                                kind=EdgeKind.IMPORTS,
                            )
                        )
                elif child.type == "class_definition":
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _slice_text(source, name_node)
                    cls_id = symbols.get(name)
                    superclasses = child.child_by_field_name("superclasses")
                    if cls_id is not None and superclasses is not None:
                        for arg in superclasses.named_children:
                            base_name = _slice_text(source, arg).split(".")[-1]
                            base_id = symbols.get(base_name)
                            out.edges.append(
                                CodeEdge(
                                    src_id=cls_id,
                                    dst_id=base_id
                                    if base_id is not None
                                    else _external_symbol_dst(base_name),
                                    kind=EdgeKind.INHERITS,
                                )
                            )
                    body = child.child_by_field_name("body")
                    if body is not None:
                        _imports_and_calls(body, cls_id)
                elif child.type == "function_definition":
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _slice_text(source, name_node)
                    # Resolve this function's node id. Free functions land
                    # in ``symbols`` under their bare name; methods are
                    # registered under qualname ``Class.method``, so for
                    # the method case we synthesize the id from the
                    # enclosing class id (the enclosing_id is the class
                    # node when we recursed into a class body).
                    fid = symbols.get(name)
                    if fid is None and enclosing_id is not None:
                        candidate_qual = enclosing_id.split("::", 1)[1] + "." + name
                        fid = _id_for(self.language, path, candidate_qual)
                    body = child.child_by_field_name("body")
                    if body is not None:
                        _imports_and_calls(body, fid)
                elif child.type == "call":
                    # We're inside enclosing_id (function/method) — try to
                    # resolve callee to a same-file symbol.
                    func_node = child.child_by_field_name("function")
                    if func_node is not None and enclosing_id is not None:
                        callee_name = _slice_text(source, func_node).split(".")[-1]
                        callee_id = symbols.get(callee_name)
                        if callee_id is not None and callee_id != enclosing_id:
                            out.edges.append(
                                CodeEdge(
                                    src_id=enclosing_id,
                                    dst_id=callee_id,
                                    kind=EdgeKind.CALLS,
                                )
                            )
                    # Recurse into args / attribute receivers too.
                    _imports_and_calls(child, enclosing_id)
                else:
                    _imports_and_calls(child, enclosing_id)

        _imports_and_calls(root, None)


def _extract_python_signature(source: bytes, func_node: tree_sitter.Node) -> str | None:
    name = func_node.child_by_field_name("name")
    params = func_node.child_by_field_name("parameters")
    return_type = func_node.child_by_field_name("return_type")
    if name is None or params is None:
        return None
    sig = f"def {_slice_text(source, name)}{_slice_text(source, params)}"
    if return_type is not None:
        sig += f" -> {_slice_text(source, return_type)}"
    return sig


def _extract_python_docstring(source: bytes, def_node: tree_sitter.Node) -> str | None:
    body = def_node.child_by_field_name("body")
    if body is None:
        return None
    # tree-sitter-python emits docstrings either as a bare ``string``
    # node directly under the body or wrapped in an
    # ``expression_statement``. Only the FIRST named child can be the
    # docstring (PEP 257 convention).
    if not body.named_children:
        return None
    first = body.named_children[0]
    if first.type == "string":
        return _strip_python_string_literal(_slice_text(source, first))
    if first.type == "expression_statement":
        for sub in first.named_children:
            if sub.type == "string":
                return _strip_python_string_literal(_slice_text(source, sub))
    return None


def _strip_python_string_literal(text: str) -> str:
    text = text.strip()
    for q in ('"""', "'''", '"', "'"):
        if text.startswith(q) and text.endswith(q) and len(text) >= 2 * len(q):
            return text[len(q) : -len(q)].strip()
    return text


def _extract_python_import_names(source: bytes, import_node: tree_sitter.Node) -> list[str]:
    names: list[str] = []
    for child in import_node.named_children:
        if child.type in {"dotted_name", "aliased_import"}:
            target = child
            if child.type == "aliased_import":
                inner = child.child_by_field_name("name")
                if inner is not None:
                    target = inner
            names.append(_slice_text(source, target))
    return names


def _imports_dst(module_name: str) -> str:
    """Stable IMPORTS edge target — namespaces external module references.

    Same-workspace cross-file imports could later be rewired by the
    graph builder when it sees a matching module node; for E20 we keep
    the simple "external" namespace so the dst id is deterministic.
    """
    return f"external:python::{module_name}"


def _external_symbol_dst(name: str) -> str:
    return f"external:python::symbol/{name}"


# ---------------------------------------------------------------------------
# TypeScript / JavaScript strategy
# ---------------------------------------------------------------------------
_TS_CLASS_NODE_TYPES = {"class_declaration", "abstract_class_declaration"}
_TS_FUNCTION_NODE_TYPES = {
    "function_declaration",
    "generator_function_declaration",
    "method_definition",
}


class _TypescriptLikeStrategy(_LanguageStrategy):
    """Shared extraction for TypeScript, TSX, and JavaScript.

    The three grammars are similar enough that one strategy covers them
    when we limit the extraction to the obvious-named productions.
    """

    def emit(self, path: str, source: bytes, root: tree_sitter.Node, out: ParseResult) -> None:
        module_id = _id_for(self.language, path, "module")
        symbols: dict[str, str] = {}

        def _collect(node: tree_sitter.Node, parent_qual: str | None) -> None:
            for child in node.named_children:
                ctype = child.type
                if ctype == "import_statement":
                    src_node = child.child_by_field_name("source")
                    if src_node is not None:
                        # Source is a string node; strip quotes.
                        raw = _slice_text(source, src_node).strip().strip("'").strip('"')
                        out.edges.append(
                            CodeEdge(
                                src_id=module_id,
                                dst_id=f"external:{self.language}::{raw}",
                                kind=EdgeKind.IMPORTS,
                            )
                        )
                elif ctype in _TS_CLASS_NODE_TYPES:
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _slice_text(source, name_node)
                    qual = name if parent_qual is None else f"{parent_qual}.{name}"
                    nid = _id_for(self.language, path, qual)
                    start_line, end_line = _node_lines(child)
                    out.nodes.append(
                        CodeNode(
                            id=nid,
                            kind=NodeKind.CLASS,
                            name=name,
                            path=path,
                            start_line=start_line,
                            end_line=end_line,
                            signature=None,
                            docstring=None,
                            parent_id=module_id,
                            language=self.language,
                        )
                    )
                    symbols[name] = nid
                    # Recurse into class body for methods.
                    body = child.child_by_field_name("body")
                    if body is not None:
                        _collect(body, qual)
                elif ctype in _TS_FUNCTION_NODE_TYPES:
                    name_node = child.child_by_field_name("name")
                    if name_node is None:
                        continue
                    name = _slice_text(source, name_node)
                    qual = name if parent_qual is None else f"{parent_qual}.{name}"
                    nid = _id_for(self.language, path, qual)
                    kind = NodeKind.METHOD if parent_qual is not None else NodeKind.FUNCTION
                    parent_id = (
                        _id_for(self.language, path, parent_qual)
                        if parent_qual is not None
                        else module_id
                    )
                    start_line, end_line = _node_lines(child)
                    out.nodes.append(
                        CodeNode(
                            id=nid,
                            kind=kind,
                            name=name,
                            path=path,
                            start_line=start_line,
                            end_line=end_line,
                            signature=_extract_ts_signature(source, child),
                            docstring=None,
                            parent_id=parent_id,
                            language=self.language,
                        )
                    )
                    symbols[name] = nid
                else:
                    _collect(child, parent_qual)

        _collect(root, None)


def _extract_ts_signature(source: bytes, fn_node: tree_sitter.Node) -> str | None:
    name_node = fn_node.child_by_field_name("name")
    params = fn_node.child_by_field_name("parameters")
    if name_node is None or params is None:
        return None
    return f"{_slice_text(source, name_node)}{_slice_text(source, params)}"


# ---------------------------------------------------------------------------
# Markdown strategy
# ---------------------------------------------------------------------------
_WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")


class _MarkdownStrategy(_LanguageStrategy):
    """Headings → ``doc_section`` nodes. Wikilinks → ``doc_references`` edges.

    Tree-sitter-markdown's grammar emits ``atx_heading`` for headings.
    We treat each heading as the start of a section that runs until the
    next heading at the same or higher level — for E20's coarse grain,
    walking to next heading at any level is enough (founder's docs
    rarely nest deeper than h3).
    """

    def emit(self, path: str, source: bytes, root: tree_sitter.Node, out: ParseResult) -> None:
        module_id = _id_for(self.language, path, "module")
        sections: list[tuple[str, str, int]] = []  # (heading_text, node_id, start_line)
        # Walk the tree to find all atx_heading nodes; treat their text
        # content as the section title.
        stack: list[tree_sitter.Node] = [root]
        while stack:
            node = stack.pop(0)
            if node.type == "atx_heading":
                # Heading text is in inline node child(ren).
                heading_text = _slice_text(source, node).lstrip("#").strip()
                if heading_text:
                    qual = f"section/{_slugify_heading(heading_text)}"
                    nid = _id_for(self.language, path, qual)
                    start_line, end_line = _node_lines(node)
                    out.nodes.append(
                        CodeNode(
                            id=nid,
                            kind=NodeKind.DOC_SECTION,
                            name=heading_text,
                            path=path,
                            start_line=start_line,
                            end_line=end_line,
                            signature=None,
                            docstring=None,
                            parent_id=module_id,
                            language=self.language,
                        )
                    )
                    sections.append((heading_text, nid, start_line))
            for child in node.named_children:
                stack.append(child)

        # Wikilinks: walk the source text once, attribute each match to
        # the most recent heading (or to the module if no heading
        # preceded it). Tree-sitter-markdown doesn't enrich wikilinks
        # natively, so a regex over the raw source is the pragmatic
        # extractor — same approach Obsidian + downstream tools take.
        try:
            text = source.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            return
        # Build (start_line, section_id) for each wikilink attribution.
        sections_sorted = sorted(sections, key=lambda s: s[2])
        for match in _WIKILINK_RE.finditer(text):
            target = match.group(1).strip()
            if not target:
                continue
            # 1-based line for the match.
            line_no = text.count("\n", 0, match.start()) + 1
            # Find most recent heading at-or-before line_no.
            src_id = module_id
            for _, sec_id, sec_line in sections_sorted:
                if sec_line <= line_no:
                    src_id = sec_id
                else:
                    break
            out.edges.append(
                CodeEdge(
                    src_id=src_id,
                    dst_id=f"wiki::{target}",
                    kind=EdgeKind.DOC_REFERENCES,
                )
            )


def _slugify_heading(text: str) -> str:
    """Turn a heading into a stable, URL-friendly path fragment."""
    slug = text.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s]+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-") or "untitled"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
_LANGUAGE_STRATEGIES: dict[str, _LanguageStrategy] = {
    "python": _PythonStrategy("python"),
    "typescript": _TypescriptLikeStrategy("typescript"),
    "tsx": _TypescriptLikeStrategy("tsx"),
    "javascript": _TypescriptLikeStrategy("javascript"),
    "markdown": _MarkdownStrategy("markdown"),
}


def parse_source(*, path: str, source: bytes, language: str) -> ParseResult:
    """Parse one source file into typed nodes/edges.

    ``path`` is the workspace-stable POSIX-style path; ``source`` is the
    raw bytes; ``language`` must be one of :data:`SUPPORTED_LANGUAGES`.

    Always returns at least the module-level :class:`CodeNode` so callers
    can always count files. A syntax error never raises — tree-sitter
    parses partially; whatever named definitions it recognized still
    flow through the strategy.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {language!r}")
    parser = _get_parser(language)
    tree = parser.parse(source)
    root = tree.root_node
    out = ParseResult()
    # Module node — always present, even when the file is empty / broken.
    module_id = _id_for(language, path, "module")
    end_line = max(1, root.end_point[0] + 1)
    out.nodes.append(
        CodeNode(
            id=module_id,
            kind=NodeKind.MODULE,
            name=PurePosixPath(path).name,
            path=path,
            start_line=1,
            end_line=end_line,
            signature=None,
            docstring=None,
            parent_id=None,
            language=language,
        )
    )
    strategy = _LANGUAGE_STRATEGIES[language]
    try:
        strategy.emit(path, source, root, out)
    except Exception:  # noqa: BLE001 — one bad file must not crash the bootstrap
        logger.warning(
            "code_graph_parser_emit_failed",
            path=path,
            language=language,
            exc_info=True,
        )
    return out


__all__ = [
    "SUPPORTED_LANGUAGES",
    "detect_language",
    "parse_source",
]
