"""Tests for backend.knowledge.ingest.index_reader — NoteSummary dataclass."""

from backend.knowledge.ingest.index_reader import NoteSummary


class TestNoteSummaryDefaults:
    """Test NoteSummary creation with default values."""

    def test_required_fields_only(self) -> None:
        summary = NoteSummary(path="garden/idea/test.md", title="Test", note_type="idea")
        assert summary.path == "garden/idea/test.md"
        assert summary.title == "Test"
        assert summary.note_type == "idea"

    def test_default_tags_empty_list(self) -> None:
        summary = NoteSummary(path="seeds/raw.md", title="Raw", note_type="seed")
        assert summary.tags == []

    def test_default_source_empty_string(self) -> None:
        summary = NoteSummary(path="seeds/raw.md", title="Raw", note_type="seed")
        assert summary.source == ""

    def test_default_captured_at_empty_string(self) -> None:
        summary = NoteSummary(path="seeds/raw.md", title="Raw", note_type="seed")
        assert summary.captured_at == ""

    def test_default_related_empty_list(self) -> None:
        summary = NoteSummary(path="seeds/raw.md", title="Raw", note_type="seed")
        assert summary.related == []


class TestNoteSummaryCustomValues:
    """Test NoteSummary creation with custom values."""

    def test_all_fields_populated(self) -> None:
        summary = NoteSummary(
            path="garden/insight/weekly.md",
            title="Weekly Digest",
            note_type="insight",
            tags=["weekly", "summary"],
            source="telegram-input",
            captured_at="2026-03-11",
            related=["[[BSage]]", "[[Planning]]"],
        )
        assert summary.path == "garden/insight/weekly.md"
        assert summary.title == "Weekly Digest"
        assert summary.note_type == "insight"
        assert summary.tags == ["weekly", "summary"]
        assert summary.source == "telegram-input"
        assert summary.captured_at == "2026-03-11"
        assert summary.related == ["[[BSage]]", "[[Planning]]"]

    def test_tags_are_independent_between_instances(self) -> None:
        """Each instance should have its own tags list (no shared mutable default)."""
        s1 = NoteSummary(path="a.md", title="A", note_type="idea")
        s2 = NoteSummary(path="b.md", title="B", note_type="idea")
        s1.tags.append("modified")
        assert s2.tags == []

    def test_related_are_independent_between_instances(self) -> None:
        """Each instance should have its own related list."""
        s1 = NoteSummary(path="a.md", title="A", note_type="idea")
        s2 = NoteSummary(path="b.md", title="B", note_type="idea")
        s1.related.append("[[X]]")
        assert s2.related == []

    def test_equality(self) -> None:
        kwargs = {
            "path": "garden/idea/x.md",
            "title": "X",
            "note_type": "idea",
            "tags": ["a"],
            "source": "test",
            "captured_at": "2026-01-01",
            "related": ["[[Y]]"],
        }
        assert NoteSummary(**kwargs) == NoteSummary(**kwargs)

    def test_inequality_on_different_path(self) -> None:
        s1 = NoteSummary(path="a.md", title="A", note_type="idea")
        s2 = NoteSummary(path="b.md", title="A", note_type="idea")
        assert s1 != s2
