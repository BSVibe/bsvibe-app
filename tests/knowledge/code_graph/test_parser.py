"""Tests for backend.knowledge.code_graph.parser — Lift E20 Phase B.

The parser turns one file (path + bytes) into a list of nodes + edges.
Each language has its own extraction rules but the output shape is
uniform — modules, classes, methods, functions, imports, calls,
inherits, doc references.
"""

from __future__ import annotations

import textwrap

import pytest

from backend.knowledge.code_graph.parser import (
    SUPPORTED_LANGUAGES,
    detect_language,
    parse_source,
)
from backend.knowledge.code_graph.types import EdgeKind, NodeKind


class TestLanguageDetection:
    def test_detect_python(self) -> None:
        assert detect_language("backend/api.py") == "python"
        assert detect_language("foo/bar/baz.pyi") == "python"

    def test_detect_typescript(self) -> None:
        assert detect_language("src/App.tsx") == "tsx"
        assert detect_language("backend/types.ts") == "typescript"

    def test_detect_javascript(self) -> None:
        assert detect_language("src/index.js") == "javascript"
        assert detect_language("scripts/x.mjs") == "javascript"

    def test_detect_markdown(self) -> None:
        assert detect_language("README.md") == "markdown"
        assert detect_language("docs/spec.MD") == "markdown"

    def test_detect_unsupported(self) -> None:
        assert detect_language("Cargo.toml") is None
        assert detect_language("data.json") is None

    def test_supported_languages_set(self) -> None:
        assert SUPPORTED_LANGUAGES == {"python", "typescript", "tsx", "javascript", "markdown"}


class TestPythonParser:
    def test_module_emits_module_node(self) -> None:
        src = b"x = 1\n"
        result = parse_source(path="src/foo.py", source=src, language="python")
        module = next(n for n in result.nodes if n.kind is NodeKind.MODULE)
        assert module.path == "src/foo.py"
        assert module.id == "python:src/foo.py::module"
        assert module.language == "python"

    def test_function_node(self) -> None:
        src = textwrap.dedent(
            '''
            def hello(name: str) -> str:
                """Say hello to NAME."""
                return f"hi {name}"
            '''
        ).encode("utf-8")
        result = parse_source(path="src/foo.py", source=src, language="python")
        funcs = [n for n in result.nodes if n.kind is NodeKind.FUNCTION]
        assert len(funcs) == 1
        f = funcs[0]
        assert f.name == "hello"
        assert f.id.endswith("::hello")
        assert f.docstring is not None and "NAME" in f.docstring
        assert f.signature is not None and "name: str" in f.signature

    def test_class_with_method(self) -> None:
        src = textwrap.dedent(
            """
            class Foo(Base):
                def bar(self):
                    pass
            """
        ).encode("utf-8")
        result = parse_source(path="src/foo.py", source=src, language="python")
        classes = [n for n in result.nodes if n.kind is NodeKind.CLASS]
        methods = [n for n in result.nodes if n.kind is NodeKind.METHOD]
        assert len(classes) == 1
        assert classes[0].name == "Foo"
        assert len(methods) == 1
        assert methods[0].name == "bar"
        # parent_id of method points at the class node id.
        assert methods[0].parent_id == classes[0].id

    def test_import_edges(self) -> None:
        src = b"import os\nfrom pathlib import Path\n"
        result = parse_source(path="a.py", source=src, language="python")
        imports = [e for e in result.edges if e.kind is EdgeKind.IMPORTS]
        # Every import edge starts at the module node.
        module_id = "python:a.py::module"
        assert all(e.src_id == module_id for e in imports)
        # Targets carry the imported module path so a future cross-file
        # resolver can match same workspace.
        dst_ids = sorted(e.dst_id for e in imports)
        assert any("os" in d for d in dst_ids)
        assert any("pathlib" in d for d in dst_ids)

    def test_inherits_edge(self) -> None:
        src = b"class Foo(Bar):\n    pass\n"
        result = parse_source(path="a.py", source=src, language="python")
        inherits = [e for e in result.edges if e.kind is EdgeKind.INHERITS]
        assert len(inherits) == 1

    def test_call_edge_inside_function(self) -> None:
        src = textwrap.dedent(
            """
            def helper():
                pass

            def caller():
                helper()
            """
        ).encode("utf-8")
        result = parse_source(path="a.py", source=src, language="python")
        calls = [e for e in result.edges if e.kind is EdgeKind.CALLS]
        # We resolve same-file references — helper is in the parsed set
        # so caller→helper must be an edge.
        assert any(e.dst_id.endswith("::helper") for e in calls)


class TestTypescriptParser:
    def test_function_and_class_extracted(self) -> None:
        src = textwrap.dedent(
            """
            function greet(name: string): string {
                return `hi ${name}`;
            }

            export class Box {
                constructor(public size: number) {}
                inflate() { return this.size; }
            }
            """
        ).encode("utf-8")
        result = parse_source(path="src/box.ts", source=src, language="typescript")
        funcs = [n for n in result.nodes if n.kind is NodeKind.FUNCTION]
        classes = [n for n in result.nodes if n.kind is NodeKind.CLASS]
        methods = [n for n in result.nodes if n.kind is NodeKind.METHOD]
        assert any(n.name == "greet" for n in funcs)
        assert any(n.name == "Box" for n in classes)
        # methods include inflate and the constructor; we accept either name.
        assert any(n.name in {"inflate", "constructor"} for n in methods)

    def test_import_edges(self) -> None:
        src = b"import { foo } from './foo';\nimport bar from 'lib/bar';\n"
        result = parse_source(path="a.ts", source=src, language="typescript")
        imports = [e for e in result.edges if e.kind is EdgeKind.IMPORTS]
        assert len(imports) >= 2


class TestJavascriptParser:
    def test_function_extracted(self) -> None:
        src = b"function f() { return 1; }\n"
        result = parse_source(path="a.js", source=src, language="javascript")
        funcs = [n for n in result.nodes if n.kind is NodeKind.FUNCTION]
        assert any(n.name == "f" for n in funcs)


class TestMarkdownParser:
    def test_doc_sections_and_doc_references(self) -> None:
        src = textwrap.dedent(
            """
            # Title

            Some intro about [[Concept]].

            ## Sub heading

            More text mentioning [[AnotherConcept]].
            """
        ).encode("utf-8")
        result = parse_source(path="README.md", source=src, language="markdown")
        sections = [n for n in result.nodes if n.kind is NodeKind.DOC_SECTION]
        # The module itself + at least 2 section nodes (the two headings).
        assert len(sections) >= 2
        # Wikilinks → doc_references edges from the containing section.
        doc_refs = [e for e in result.edges if e.kind is EdgeKind.DOC_REFERENCES]
        targets = sorted(e.dst_id for e in doc_refs)
        assert any("Concept" in t for t in targets)
        assert any("AnotherConcept" in t for t in targets)


class TestParserRobustness:
    def test_syntax_error_returns_empty_or_partial(self) -> None:
        # Garbage doesn't crash the parser; we get an empty/partial result.
        result = parse_source(
            path="a.py",
            source=b"def )( syntax error :::\n",
            language="python",
        )
        # At minimum the module node still exists; no crash.
        assert any(n.kind is NodeKind.MODULE for n in result.nodes)

    def test_empty_file(self) -> None:
        result = parse_source(path="a.py", source=b"", language="python")
        assert any(n.kind is NodeKind.MODULE for n in result.nodes)

    def test_invalid_language_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_source(path="a.x", source=b"", language="cobol")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
