"""Selector buckets — manifests / docs / source / skip."""

from __future__ import annotations

import pytest

from backend.products.application.bootstrap.selector import FileBucket, classify


@pytest.mark.parametrize(
    "rel_path,expected",
    [
        ("pyproject.toml", FileBucket.STRUCTURAL_MANIFEST),
        ("apps/api/package.json", FileBucket.STRUCTURAL_MANIFEST),
        ("backend/Cargo.toml", FileBucket.STRUCTURAL_MANIFEST),
        ("Dockerfile", FileBucket.STRUCTURAL_MANIFEST),
        ("Makefile", FileBucket.STRUCTURAL_MANIFEST),
    ],
)
def test_manifest_files_bucket(rel_path, expected):
    assert classify(rel_path) is expected


@pytest.mark.parametrize(
    "rel_path",
    [
        "package-lock.json",
        "yarn.lock",
        "Cargo.lock",
        "uv.lock",
        "Pipfile.lock",
    ],
)
def test_lockfiles_skip(rel_path):
    assert classify(rel_path) is FileBucket.SKIP


@pytest.mark.parametrize(
    "rel_path,expected",
    [
        ("README.md", FileBucket.STRUCTURAL_DOC),
        ("README", FileBucket.STRUCTURAL_DOC),
        ("ARCHITECTURE.md", FileBucket.STRUCTURAL_DOC),
        ("CONTRIBUTING.md", FileBucket.STRUCTURAL_DOC),
        ("CLAUDE.md", FileBucket.STRUCTURAL_DOC),
        (".bsvibe/PRODUCT.md", FileBucket.STRUCTURAL_DOC),
    ],
)
def test_top_level_docs_bucket(rel_path, expected):
    assert classify(rel_path) is expected


def test_sub_dir_readme_is_not_promoted():
    # A README under a sub-dir is just markdown — falls through to source if
    # ``.md`` is a source ext (it isn't, so this should SKIP — markdown isn't
    # in the source allowlist). The point is: NOT promoted to STRUCTURAL_DOC.
    assert classify("backend/README.md") is not FileBucket.STRUCTURAL_DOC


@pytest.mark.parametrize(
    "rel_path",
    [
        "backend/app.py",
        "src/lib/main.rs",
        "apps/pwa/components/foo.tsx",
        "cmd/main.go",
        "lib/widget.kt",
    ],
)
def test_source_extensions_bucket(rel_path):
    assert classify(rel_path) is FileBucket.SOURCE


@pytest.mark.parametrize(
    "rel_path",
    [
        ".env.example",
        "logo.png",
        "assets/icon.svg",
        "data/sample.csv",
        "backend/README.md",
    ],
)
def test_unknown_kinds_skip(rel_path):
    assert classify(rel_path) is FileBucket.SKIP
