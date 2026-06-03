"""Extractors — file tree, manifests, docs + source collector."""

from __future__ import annotations

from pathlib import Path

from backend.products.application.bootstrap.extractors import (
    docs,
    file_tree,
    manifests,
)
from backend.products.application.bootstrap.source_collector import (
    collect_source_artifacts,
)
from backend.products.application.bootstrap.walker import WalkedFile


def _walked(root: Path, rel: str, content: bytes = b"x\n") -> WalkedFile:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return WalkedFile(abs_path=p, rel_path=rel, size=len(content))


def test_file_tree_artifact_shape(tmp_path):
    walked = [
        _walked(tmp_path, "backend/app.py"),
        _walked(tmp_path, "apps/pwa/index.tsx"),
        _walked(tmp_path, "README.md"),
    ]
    art = file_tree(walked)
    assert art["kind"] == "file-tree"
    assert art["label"] == "repo/file-tree.md"
    content = art["content"]
    assert "Repository file tree" in content
    assert "backend" in content
    assert "apps" in content
    assert "README.md" in content


def test_file_tree_handles_empty_repo(tmp_path):
    art = file_tree([])
    assert art["kind"] == "file-tree"
    assert "no walked files" in art["content"]


def test_manifests_emit_one_per_manifest(tmp_path):
    walked = [
        _walked(tmp_path, "pyproject.toml", b"[project]\nname='x'\n"),
        _walked(tmp_path, "apps/pwa/package.json", b'{"name":"pwa"}\n'),
        _walked(tmp_path, "backend/app.py", b"print()\n"),
    ]
    arts = manifests(walked)
    assert len(arts) == 2
    labels = {a["label"] for a in arts}
    assert labels == {"pyproject.toml", "apps/pwa/package.json"}
    for a in arts:
        assert a["kind"] == "manifest"
        assert a["content"].startswith(f"# Manifest: {a['label']}")


def test_docs_extractor(tmp_path):
    walked = [
        _walked(tmp_path, "README.md", b"# my app\n"),
        _walked(tmp_path, "ARCHITECTURE.md", b"# arch\n"),
        _walked(tmp_path, "backend/app.py", b"x=1\n"),
    ]
    arts = docs(walked)
    assert len(arts) == 2
    labels = {a["label"] for a in arts}
    assert labels == {"README.md", "ARCHITECTURE.md"}
    for a in arts:
        assert a["kind"] == "doc"
        assert a["content"].startswith(f"# Doc: {a['label']}")


def test_source_collector_emits_source_with_file_header(tmp_path):
    walked = [
        _walked(tmp_path, "backend/app.py", b"print('hi')\n"),
        _walked(tmp_path, "apps/pwa/main.ts", b"console.log(1)\n"),
        _walked(tmp_path, "README.md", b"# doc\n"),
        _walked(tmp_path, "pyproject.toml", b"[project]\n"),
    ]
    arts = list(collect_source_artifacts(walked))
    assert len(arts) == 2
    labels = {a["label"] for a in arts}
    assert labels == {"backend/app.py", "apps/pwa/main.ts"}
    for a in arts:
        assert a["kind"] == "source"
        assert a["content"].startswith(f"# File: {a['label']}")


def test_source_collector_caps_oversize(tmp_path):
    # Beyond cap — should still emit ONE artifact, content truncated with marker.
    big = b"x" * 200_000
    walked = [_walked(tmp_path, "backend/big.py", big)]
    arts = list(collect_source_artifacts(walked))
    assert len(arts) == 1
    # cap is 128_000; the artifact content fits within cap + header + marker.
    assert "truncated for ingest seed cap" in arts[0]["content"]
