"""Smoke + delta tests for Lift L1 — writer_core decomposition.

Asserts:

1. The package import path is unchanged: ``from backend.knowledge.graph.writer_core
   import GardenWriter, GardenNote`` still works after the file→package split.
2. ``writer_core`` is now a package (the old monolithic .py was deleted).
3. Each sub-file in the package is <= 350 LOC.
4. The public surface (``GardenWriter`` + ``GardenNote``) round-trips a basic
   write_seed call through the composed mixins.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer_core import GardenNote, GardenWriter

MAX_FILE_LOC = 350


def test_writer_core_is_a_package() -> None:
    """writer_core must be a package (directory with __init__.py), not a single file."""
    mod = importlib.import_module("backend.knowledge.graph.writer_core")
    file_attr = getattr(mod, "__file__", "")
    assert file_attr.endswith("__init__.py"), (
        f"writer_core should be a package, got module file {file_attr!r}"
    )


def test_writer_core_public_api_exports() -> None:
    """Public symbols re-exported by the package facade remain importable."""
    assert GardenWriter is not None
    assert GardenNote is not None
    # Hardening sprint mixins should remain importable for internal callers.
    from backend.knowledge.graph.writer_core import (  # noqa: F401
        _WriterIOMixin,
        _WriterMutationMixin,
        _WriterToolHandlersMixin,
    )


def test_writer_core_subfiles_under_loc_cap() -> None:
    """Each sub-file in the writer_core package must be <= 350 LOC."""
    pkg = importlib.import_module("backend.knowledge.graph.writer_core")
    pkg_dir = Path(pkg.__file__).parent  # type: ignore[arg-type]
    py_files = sorted(pkg_dir.glob("*.py"))
    assert py_files, "writer_core package should contain at least one .py file"
    oversized: list[tuple[str, int]] = []
    for f in py_files:
        loc = sum(1 for _ in f.read_text(encoding="utf-8").splitlines())
        if loc > MAX_FILE_LOC:
            oversized.append((f.name, loc))
    assert not oversized, f"Files exceed {MAX_FILE_LOC} LOC cap: {oversized}"


@pytest.mark.asyncio
async def test_garden_writer_write_seed_smoke(tmp_path: Path) -> None:
    """Compose GardenWriter, call a basic IO mixin method, assert the file lands."""
    vault = Vault(tmp_path)
    vault.ensure_dirs()
    writer = GardenWriter(vault)

    result = await writer.write_seed("calendar", {"summary": "hello"})

    assert result.exists()
    assert result.suffix == ".md"
    content = result.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "type: seed" in content
    assert "source: calendar" in content


@pytest.mark.asyncio
async def test_garden_writer_write_garden_smoke(tmp_path: Path) -> None:
    """write_garden composed via the IO mixin still produces a maturity-folder note."""
    vault = Vault(tmp_path)
    vault.ensure_dirs()
    writer = GardenWriter(vault)

    note = GardenNote(
        title="Test Note",
        content="body",
        note_type="idea",
        source="t",
    )
    result = await writer.write_garden(note)

    assert result.exists()
    rel = result.relative_to(tmp_path)
    # _resolve_folder maps seedling maturity → garden/seedling
    assert str(rel).startswith("garden/seedling/")


@pytest.mark.asyncio
async def test_garden_writer_tool_handler_smoke(tmp_path: Path) -> None:
    """handle_write_note (tool-handler mixin) still routes through write_garden."""
    vault = Vault(tmp_path)
    vault.ensure_dirs()
    writer = GardenWriter(vault)

    result = await writer.handle_write_note({"title": "T", "content": "B"})

    assert result["status"] == "saved"
    assert result["title"] == "T"
    assert Path(result["path"]).exists()
