"""Tests for backend.knowledge.code_graph.pipeline — Lift E20 Phase C.

Integration of the four phases on a small fake repo: filter → parse →
build graph → run communities → emit one artifact per community for the
LLM, plus per-doc artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.knowledge.code_graph.pipeline import (
    build_code_graph_artifacts,
    is_test_path,
    persist_graph,
)


def _seed_repo(tmp_path: Path) -> Path:
    """Create a small fake repo with mixed signal."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text(
        '''\
"""Foo module."""

def util():
    """Internal helper."""
    return 1


def caller():
    """Calls util."""
    return util() + 1
'''
    )
    (tmp_path / "src" / "bar.py").write_text(
        """\
from foo import util


class Bar:
    \"\"\"A bar.\"\"\"

    def shake(self):
        return util()
"""
    )
    (tmp_path / "README.md").write_text("# My Project\n\nAbout [[Concept]] and [[Tooling]].\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_util(): assert True\n")
    # lockfile — should be filtered.
    (tmp_path / "package-lock.json").write_text('{"name": "x"}\n')
    # binary.
    (tmp_path / "logo.png").write_bytes(b"\x89PNG fake\n")
    return tmp_path


class TestIsTestPath:
    def test_pytest_style(self) -> None:
        assert is_test_path("tests/test_foo.py")
        assert is_test_path("backend/tests/test_x.py")

    def test_jest_style(self) -> None:
        assert is_test_path("apps/web/src/x.test.ts")
        assert is_test_path("apps/web/src/__tests__/comp.tsx")

    def test_go_style(self) -> None:
        assert is_test_path("internal/foo_test.go")

    def test_normal_source_is_not_test(self) -> None:
        assert not is_test_path("backend/api.py")
        assert not is_test_path("src/foo.ts")


class TestBuildCodeGraphArtifacts:
    def test_pipeline_returns_community_and_doc_artifacts(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path)
        result = build_code_graph_artifacts(repo)

        # Filter dropped lockfile + PNG.
        assert result.filter_summary.get("lockfile") == 1
        assert result.filter_summary.get("binary_extension") == 1

        # Graph has at least the module + function nodes for src/foo.py
        # and src/bar.py (4 functions + 1 class + 1 method = 6 named
        # things, plus 2 module nodes = 8 nodes minimum). Test file is
        # filtered out.
        assert result.graph.number_of_nodes() >= 6

        # At least one artifact emitted per community + per-doc.
        # README.md is the only top-level doc; it should produce one
        # ``markdown-doc`` artifact.
        kinds = {a["kind"] for a in result.artifacts}
        assert "code-graph-community" in kinds
        assert "markdown-doc" in kinds

    def test_test_files_excluded_from_graph(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path)
        result = build_code_graph_artifacts(repo)
        node_paths = {result.graph.nodes[n].get("path", "") for n in result.graph.nodes}
        assert all("tests/test_foo.py" not in p for p in node_paths)

    def test_each_community_artifact_carries_signatures(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path)
        result = build_code_graph_artifacts(repo)
        comm_artifacts = [a for a in result.artifacts if a["kind"] == "code-graph-community"]
        assert comm_artifacts
        # The content carries function/class signatures so the LLM can
        # extract patterns. We assert at least one ``def`` or ``class``
        # shows up in the rendered chunk.
        joined = "\n".join(a["content"] for a in comm_artifacts)
        assert "def " in joined or "class " in joined


class TestPersistGraph:
    def test_persist_creates_graph_json(self, tmp_path: Path) -> None:
        repo = _seed_repo(tmp_path)
        result = build_code_graph_artifacts(repo)
        out = tmp_path / "vault" / "code_graph" / "graph.json"
        persist_graph(result.graph, out)
        assert out.exists()
        body = json.loads(out.read_text())
        assert body["nodes"]
        assert isinstance(body["edges"], list)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
