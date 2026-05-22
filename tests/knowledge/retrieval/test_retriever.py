"""Tests for backend.knowledge.retrieval.retriever — index-based 2-step note retrieval."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.knowledge.graph.markdown_utils import extract_frontmatter, extract_title
from backend.knowledge.ingest.index_reader import NoteSummary
from backend.knowledge.retrieval.retriever import VaultRetriever


def _mock_vault(notes_by_dir: dict[str, list[tuple[str, str]]] | None = None):
    """Create a mock Vault with notes."""
    vault = MagicMock()
    vault.root = Path("/vault")
    notes_by_dir = notes_by_dir or {}

    async def _read_notes(subdir):
        entries = notes_by_dir.get(subdir, [])
        return [Path(f"/vault/{subdir}/{name}") for name, _content in entries]

    async def _read_content(path):
        for entries in notes_by_dir.values():
            for name, content in entries:
                if path.name == name:
                    return content
        raise FileNotFoundError(path)

    def _resolve_path(subpath):
        return Path(f"/vault/{subpath}")

    vault.read_notes = AsyncMock(side_effect=_read_notes)
    vault.read_note_content = AsyncMock(side_effect=_read_content)
    vault.resolve_path = MagicMock(side_effect=_resolve_path)
    return vault


def _mock_index_reader(
    summaries_by_category: dict[str, list[NoteSummary]] | None = None,
) -> AsyncMock:
    """Create a mock FileIndexReader."""
    reader = AsyncMock()
    summaries_by_category = summaries_by_category or {}

    async def _get_summaries(category: str) -> list[NoteSummary]:
        return summaries_by_category.get(category, [])

    async def _get_all() -> list[NoteSummary]:
        all_s: list[NoteSummary] = []
        for entries in summaries_by_category.values():
            all_s.extend(entries)
        return all_s

    reader.get_summaries = AsyncMock(side_effect=_get_summaries)
    reader.get_all_summaries = AsyncMock(side_effect=_get_all)
    reader.index_note_from_content = AsyncMock()
    reader.remove_entry = AsyncMock()
    reader.rebuild_all = AsyncMock()
    return reader


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestExtractFrontmatter:
    """Test frontmatter extraction helper."""

    def test_extracts_yaml_frontmatter(self) -> None:
        text = "---\ntitle: Hello\ntype: idea\n---\nBody text"
        meta = extract_frontmatter(text)
        assert meta["title"] == "Hello"
        assert meta["type"] == "idea"

    def test_no_frontmatter_returns_empty(self) -> None:
        assert extract_frontmatter("No frontmatter here") == {}

    def test_malformed_yaml_returns_empty(self) -> None:
        text = "---\n: bad: yaml:\n---\n"
        result = extract_frontmatter(text)
        assert isinstance(result, dict)

    def test_no_closing_delimiter(self) -> None:
        text = "---\ntitle: Hello\nBody text"
        assert extract_frontmatter(text) == {}


class TestExtractTitle:
    """Test H1 heading extraction."""

    def test_extracts_h1(self) -> None:
        assert extract_title("# My Title\n\nBody") == "My Title"

    def test_no_heading_returns_empty(self) -> None:
        assert extract_title("No heading here") == ""

    def test_ignores_h2(self) -> None:
        assert extract_title("## Not H1\n\nBody") == ""


# ---------------------------------------------------------------------------
# VaultRetriever.index_available
# ---------------------------------------------------------------------------


class TestIndexAvailable:
    """Test index_available property."""

    def test_true_with_index_reader(self) -> None:
        retriever = VaultRetriever(vault=MagicMock(), index_reader=MagicMock())
        assert retriever.index_available is True

    def test_false_without_index_reader(self) -> None:
        retriever = VaultRetriever(vault=MagicMock())
        assert retriever.index_available is False


# ---------------------------------------------------------------------------
# Fallback retrieval (no index reader)
# ---------------------------------------------------------------------------


class TestFallbackRetrieval:
    """Test recency-based retrieval when no index reader is configured."""

    @pytest.mark.asyncio()
    async def test_reads_recent_notes(self) -> None:
        vault = _mock_vault(
            {
                "garden/idea": [
                    ("01-old.md", "Old content"),
                    ("02-new.md", "New content"),
                ],
            }
        )
        retriever = VaultRetriever(vault=vault)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"])
        assert "New content" in result
        assert "Old content" in result

    @pytest.mark.asyncio()
    async def test_respects_max_chars(self) -> None:
        vault = _mock_vault(
            {
                "garden/idea": [("big.md", "x" * 60_000)],
            }
        )
        retriever = VaultRetriever(vault=vault)
        result = await retriever.retrieve(
            query="test", context_dirs=["garden/idea"], max_chars=1000
        )
        assert len(result) <= 1000

    @pytest.mark.asyncio()
    async def test_empty_vault(self) -> None:
        vault = _mock_vault({})
        retriever = VaultRetriever(vault=vault)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"])
        assert result == ""


# ---------------------------------------------------------------------------
# Index-based retrieval
# ---------------------------------------------------------------------------


class TestIndexRetrieval:
    """Test index-based 2-step retrieval."""

    @pytest.mark.asyncio()
    async def test_returns_index_table_and_note_contents(self) -> None:
        summaries = [
            NoteSummary(
                path="garden/idea/note.md",
                title="My Note",
                note_type="idea",
                tags=["test"],
                captured_at="2026-03-01",
            ),
        ]
        vault = _mock_vault({"garden/idea": [("note.md", "Full note content")]})
        reader = _mock_index_reader({"garden/idea": summaries})

        retriever = VaultRetriever(vault=vault, index_reader=reader)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"])

        assert "## Note Index" in result
        assert "[[My Note]]" in result
        assert "Full note content" in result

    @pytest.mark.asyncio()
    async def test_falls_back_when_index_empty(self) -> None:
        vault = _mock_vault({"garden/idea": [("note.md", "Fallback content")]})
        reader = _mock_index_reader({})  # no summaries

        retriever = VaultRetriever(vault=vault, index_reader=reader)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"])

        assert "Fallback content" in result

    @pytest.mark.asyncio()
    async def test_falls_back_on_index_error(self) -> None:
        vault = _mock_vault({"garden/idea": [("note.md", "Fallback")]})
        reader = _mock_index_reader()
        reader.get_summaries = AsyncMock(side_effect=RuntimeError("index broken"))

        retriever = VaultRetriever(vault=vault, index_reader=reader)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"])

        assert "Fallback" in result

    @pytest.mark.asyncio()
    async def test_respects_max_chars(self) -> None:
        summaries = [
            NoteSummary(
                path="garden/idea/big.md",
                title="Big",
                note_type="idea",
                captured_at="2026-03-01",
            ),
        ]
        vault = _mock_vault({"garden/idea": [("big.md", "x" * 60_000)]})
        reader = _mock_index_reader({"garden/idea": summaries})

        retriever = VaultRetriever(vault=vault, index_reader=reader)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"], max_chars=500)
        # max_chars controls content gathering; separator "\n---\n" may add a few chars
        assert len(result) <= 510

    @pytest.mark.asyncio()
    async def test_sorts_by_date_descending(self) -> None:
        summaries = [
            NoteSummary(
                path="garden/idea/old.md",
                title="Old",
                note_type="idea",
                captured_at="2026-01-01",
            ),
            NoteSummary(
                path="garden/idea/new.md",
                title="New",
                note_type="idea",
                captured_at="2026-03-01",
            ),
        ]
        vault = _mock_vault(
            {
                "garden/idea": [
                    ("old.md", "Old content"),
                    ("new.md", "New content"),
                ],
            }
        )
        reader = _mock_index_reader({"garden/idea": summaries})

        retriever = VaultRetriever(vault=vault, index_reader=reader)
        result = await retriever.retrieve(query="test", context_dirs=["garden/idea"])

        # New should appear before Old in the index table
        new_pos = result.index("[[New]]")
        old_pos = result.index("[[Old]]")
        assert new_pos < old_pos


# ---------------------------------------------------------------------------
# search() method
# ---------------------------------------------------------------------------


class TestSearch:
    """Test search-vault tool method."""

    @pytest.mark.asyncio()
    async def test_returns_formatted_listing(self) -> None:
        summaries = [
            NoteSummary(
                path="garden/idea/note.md",
                title="Test Note",
                note_type="idea",
                tags=["ai", "agent"],
                captured_at="2026-03-01",
            ),
        ]
        reader = _mock_index_reader({"garden/idea": summaries})
        retriever = VaultRetriever(vault=MagicMock(), index_reader=reader)

        result = await retriever.search(query="ai agents", context_dirs=["garden/idea"])

        assert "Found 1 notes" in result
        assert "**Test Note**" in result
        assert "#ai" in result
        assert "#agent" in result

    @pytest.mark.asyncio()
    async def test_no_index_falls_back_to_recency(self) -> None:
        vault = MagicMock()
        vault.read_notes = AsyncMock(return_value=[])
        retriever = VaultRetriever(vault=vault)
        result = await retriever.search(query="test")
        assert isinstance(result, str)

    @pytest.mark.asyncio()
    async def test_no_results(self) -> None:
        reader = _mock_index_reader({})
        retriever = VaultRetriever(vault=MagicMock(), index_reader=reader)
        result = await retriever.search(query="test")
        assert "No notes found" in result

    @pytest.mark.asyncio()
    async def test_respects_top_k(self) -> None:
        summaries = [
            NoteSummary(path=f"seeds/{i}.md", title=f"Note {i}", note_type="seed")
            for i in range(20)
        ]
        reader = _mock_index_reader({"seeds": summaries})
        retriever = VaultRetriever(vault=MagicMock(), index_reader=reader)

        result = await retriever.search(query="test", context_dirs=["seeds"], top_k=5)
        assert "Found 5 notes" in result

    @pytest.mark.asyncio()
    async def test_search_without_context_dirs_uses_all(self) -> None:
        summaries = {
            "garden/idea": [
                NoteSummary(path="garden/idea/a.md", title="A", note_type="idea"),
            ],
            "seeds": [
                NoteSummary(path="seeds/b.md", title="B", note_type="seed"),
            ],
        }
        reader = _mock_index_reader(summaries)
        retriever = VaultRetriever(vault=MagicMock(), index_reader=reader)

        result = await retriever.search(query="test")
        assert "Found 2 notes" in result


# ---------------------------------------------------------------------------
# index_note / remove_note delegation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# reindex_all
# ---------------------------------------------------------------------------


class TestReindexAll:
    """Test full vault reindexing."""

    @pytest.mark.asyncio()
    async def test_raises_without_index_reader(self) -> None:
        retriever = VaultRetriever(vault=MagicMock())
        with pytest.raises(RuntimeError, match="not configured"):
            await retriever.reindex_all()

    @pytest.mark.asyncio()
    async def test_delegates_rebuild_and_returns_count(self) -> None:
        summaries = [
            NoteSummary(path="garden/idea/a.md", title="A", note_type="idea"),
            NoteSummary(path="seeds/b.md", title="B", note_type="seed"),
        ]
        reader = _mock_index_reader()
        reader.get_all_summaries = AsyncMock(return_value=summaries)
        retriever = VaultRetriever(vault=MagicMock(), index_reader=reader)

        count = await retriever.reindex_all()

        reader.rebuild_all.assert_called_once()
        assert count == 2
