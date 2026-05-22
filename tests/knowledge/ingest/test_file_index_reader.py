"""Tests for backend.knowledge.ingest.file_index_reader — FileIndexReader and helper functions."""

from pathlib import Path

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.ingest.file_index_reader import (
    FileIndexReader,
    _category_to_filename,
    _note_to_summary,
    _render_index_markdown,
)
from backend.knowledge.ingest.index_reader import NoteSummary

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestCategoryToFilename:
    def test_nested_category(self) -> None:
        assert _category_to_filename("garden/idea") == "garden-ideas.md"

    def test_nested_category_insight(self) -> None:
        assert _category_to_filename("garden/insight") == "garden-insights.md"

    def test_top_level_category(self) -> None:
        assert _category_to_filename("seeds") == "seeds.md"


class TestNoteToSummary:
    def test_parses_frontmatter_title(self) -> None:
        content = (
            "---\ntitle: My Note\ntype: idea\nsource: test\ntags:\n"
            "  - alpha\n  - beta\ncaptured_at: '2026-03-01'\n"
            "related:\n  - '[[BSage]]'\n---\n\nBody text."
        )
        summary = _note_to_summary("garden/idea/my-note.md", content)
        assert summary.title == "My Note"
        assert summary.note_type == "idea"
        assert summary.source == "test"
        assert summary.tags == ["alpha", "beta"]
        assert summary.captured_at == "2026-03-01"
        assert summary.related == ["[[BSage]]"]
        assert summary.path == "garden/idea/my-note.md"

    def test_falls_back_to_h1_title(self) -> None:
        content = "---\ntype: seed\n---\n\n# Heading Title\n\nBody."
        summary = _note_to_summary("seeds/note.md", content)
        assert summary.title == "Heading Title"

    def test_falls_back_to_filename_stem(self) -> None:
        content = "---\ntype: seed\n---\n\nNo heading here."
        summary = _note_to_summary("seeds/my-note.md", content)
        assert summary.title == "my-note"

    def test_no_frontmatter(self) -> None:
        content = "# Just a Title\n\nSome body text."
        summary = _note_to_summary("garden/idea/plain.md", content)
        assert summary.title == "Just a Title"
        assert summary.note_type == ""
        assert summary.tags == []

    def test_string_tags_converted_to_list(self) -> None:
        content = "---\ntags: single-tag\n---\n\n# T"
        summary = _note_to_summary("seeds/x.md", content)
        assert summary.tags == ["single-tag"]

    def test_string_related_converted_to_list(self) -> None:
        content = "---\nrelated: '[[A]]'\n---\n\n# T"
        summary = _note_to_summary("seeds/x.md", content)
        assert summary.related == ["[[A]]"]


class TestRenderAndParseRoundTrip:
    def test_render_produces_markdown_table(self) -> None:
        summaries = [
            NoteSummary(
                path="garden/idea/a.md",
                title="Alpha",
                note_type="idea",
                tags=["tag1"],
                source="test",
                captured_at="2026-03-01",
            ),
        ]
        md = _render_index_markdown("garden/idea", summaries)
        assert "| [[Alpha]] | #tag1 | test | 2026-03-01 |" in md
        assert "| Note | Tags | Source | Date |" in md
        assert "scope: garden/idea" in md

    def test_empty_summaries(self) -> None:
        md = _render_index_markdown("garden/idea", [])
        assert "total_notes: 0" in md


# ---------------------------------------------------------------------------
# FileIndexReader async tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path)
    v.ensure_dirs()
    return v


@pytest.fixture()
def reader(vault: Vault) -> FileIndexReader:
    return FileIndexReader(vault)


@pytest.mark.asyncio()
async def test_update_entry_writes_index_file(vault: Vault, reader: FileIndexReader) -> None:
    summary = NoteSummary(
        path="garden/idea/test.md",
        title="Test Note",
        note_type="idea",
        tags=["demo"],
        source="unit-test",
        captured_at="2026-03-11",
    )
    await reader.update_entry("garden/idea/test.md", summary)

    index_file = vault.root / "_index" / "garden-ideas.md"
    assert index_file.exists()
    content = index_file.read_text(encoding="utf-8")
    assert "[[Test Note]]" in content
    assert "#demo" in content


@pytest.mark.asyncio()
async def test_remove_entry_updates_index(vault: Vault, reader: FileIndexReader) -> None:
    summary = NoteSummary(
        path="garden/idea/rm.md",
        title="Remove Me",
        note_type="idea",
        captured_at="2026-01-01",
    )
    await reader.update_entry("garden/idea/rm.md", summary)
    await reader.remove_entry("garden/idea/rm.md")

    results = await reader.get_summaries("garden/idea")
    assert len(results) == 0


@pytest.mark.asyncio()
async def test_get_summaries_filters_by_category(vault: Vault, reader: FileIndexReader) -> None:
    s1 = NoteSummary(path="garden/idea/a.md", title="A", note_type="idea")
    s2 = NoteSummary(path="seeds/b.md", title="B", note_type="seed")
    await reader.update_entry("garden/idea/a.md", s1)
    await reader.update_entry("seeds/b.md", s2)

    ideas = await reader.get_summaries("garden/idea")
    assert len(ideas) == 1
    assert ideas[0].title == "A"


@pytest.mark.asyncio()
async def test_get_all_summaries_returns_everything(vault: Vault, reader: FileIndexReader) -> None:
    s1 = NoteSummary(path="garden/idea/a.md", title="A", note_type="idea")
    s2 = NoteSummary(path="seeds/b.md", title="B", note_type="seed")
    await reader.update_entry("garden/idea/a.md", s1)
    await reader.update_entry("seeds/b.md", s2)

    all_summaries = await reader.get_all_summaries()
    assert len(all_summaries) == 2


@pytest.mark.asyncio()
async def test_index_note_from_content(vault: Vault, reader: FileIndexReader) -> None:
    content = (
        "---\ntitle: Parsed\ntype: insight\ntags:\n"
        "  - auto\ncaptured_at: '2026-03-11'\n---\n\nBody."
    )
    await reader.index_note_from_content("garden/insight/parsed.md", content)

    results = await reader.get_summaries("garden/insight")
    assert len(results) == 1
    assert results[0].title == "Parsed"
    assert results[0].tags == ["auto"]


@pytest.mark.asyncio()
async def test_rebuild_scans_vault_files(vault: Vault, reader: FileIndexReader) -> None:
    # Create a note file in the vault
    idea_dir = vault.root / "garden" / "idea"
    idea_dir.mkdir(parents=True, exist_ok=True)
    note = idea_dir / "hello.md"
    note.write_text(
        "---\ntitle: Hello\ntype: idea\ntags:\n  - greet\n"
        "captured_at: '2026-03-11'\n---\n\n# Hello\n\nWorld.",
        encoding="utf-8",
    )

    await reader.rebuild("garden/idea")

    results = await reader.get_summaries("garden/idea")
    assert len(results) == 1
    assert results[0].title == "Hello"

    index_file = vault.root / "_index" / "garden-ideas.md"
    assert index_file.exists()


@pytest.mark.asyncio()
async def test_path_to_category_three_parts() -> None:
    assert FileIndexReader._path_to_category("garden/idea/my-note.md") == "garden/idea"


@pytest.mark.asyncio()
async def test_path_to_category_two_parts() -> None:
    assert FileIndexReader._path_to_category("seeds/raw.md") == "seeds"


@pytest.mark.asyncio()
async def test_path_to_category_single_part() -> None:
    assert FileIndexReader._path_to_category("orphan.md") == ""


# ---------------------------------------------------------------------------
# _loaded flag behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_loaded_flag_stays_false_on_empty_vault(tmp_path: Path) -> None:
    """When vault is empty, _loaded stays False so next call retries."""
    vault = Vault(tmp_path)
    reader = FileIndexReader(vault=vault)

    await reader._ensure_loaded()
    assert reader._loaded is False  # no entries found → not marked loaded

    # Second call should retry (not short-circuit)
    # Create a note before second call (using the maturity-based layout).
    seedling_dir = tmp_path / "garden" / "seedling"
    seedling_dir.mkdir(parents=True)
    (seedling_dir / "test.md").write_text(
        "---\ntitle: Test\nmaturity: seedling\n---\n", encoding="utf-8"
    )

    await reader._ensure_loaded()
    assert reader._loaded is True
    summaries = await reader.get_summaries("garden/seedling")
    assert len(summaries) == 1


@pytest.mark.asyncio()
async def test_loaded_flag_true_when_entries_found(tmp_path: Path) -> None:
    """When vault has notes, _loaded becomes True after first load."""
    vault = Vault(tmp_path)
    seedling_dir = tmp_path / "garden" / "seedling"
    seedling_dir.mkdir(parents=True)
    (seedling_dir / "note.md").write_text(
        "---\ntitle: A\nmaturity: seedling\n---\n", encoding="utf-8"
    )

    reader = FileIndexReader(vault=vault)
    await reader._ensure_loaded()
    assert reader._loaded is True
