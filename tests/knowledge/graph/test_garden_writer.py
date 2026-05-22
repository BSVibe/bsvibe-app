"""Tests for bsage.garden.writer — GardenWriter and GardenNote."""

from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock

import pytest

from backend.knowledge.graph.sync import SyncBackend, SyncManager, WriteEventType
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer_core import GardenNote, GardenWriter


class TestGardenNote:
    """Test GardenNote dataclass."""

    def test_garden_note_defaults(self) -> None:
        """GardenNote should have empty defaults for related and tags."""
        note = GardenNote(
            title="Test Note",
            content="Some content",
            note_type="idea",
            source="test-skill",
        )
        assert note.title == "Test Note"
        assert note.related == []
        assert note.tags == []


class TestWriteSeed:
    """Test GardenWriter.write_seed creates files with frontmatter."""

    @pytest.mark.asyncio
    async def test_write_seed_creates_file_with_frontmatter(self, tmp_path: Path) -> None:
        """write_seed should create a markdown file with YAML frontmatter."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        data = {"summary": "Team standup", "attendees": ["Alice", "Bob"]}
        result = await writer.write_seed("calendar", data)

        assert result.exists()
        assert result.suffix == ".md"

        content = result.read_text()
        assert content.startswith("---\n")
        assert "type: seed" in content
        assert "source: calendar" in content
        assert "captured_at:" in content
        assert "---" in content.split("---\n", 2)[2] or content.count("---") >= 2

    @pytest.mark.asyncio
    async def test_write_seed_creates_source_subdirectory(self, tmp_path: Path) -> None:
        """write_seed should create the seeds/{source}/ directory if it doesn't exist."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        data = {"event": "meeting"}
        result = await writer.write_seed("google-calendar", data)

        assert (tmp_path / "seeds" / "google-calendar").is_dir()
        assert result.parent == tmp_path / "seeds" / "google-calendar"

    @pytest.mark.asyncio
    async def test_write_seed_contains_data(self, tmp_path: Path) -> None:
        """write_seed should include the data in the file body."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        data = {"summary": "Important meeting", "location": "Room 42"}
        result = await writer.write_seed("calendar", data)

        content = result.read_text()
        assert "summary" in content
        assert "Important meeting" in content


class TestWriteGarden:
    """Test GardenWriter.write_garden creates notes with frontmatter."""

    @pytest.mark.asyncio
    async def test_write_garden_creates_note_with_frontmatter(self, tmp_path: Path) -> None:
        """write_garden should create a note in garden/{note_type}/ with frontmatter."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(
            title="My Great Idea",
            content="This is an idea about something.",
            note_type="idea",
            source="garden-writer",
            related=["BSage"],
        )
        result = await writer.write_garden(note)

        assert result.exists()
        # Maturity-based layout: garden/seedling for fresh captures.
        assert result.parent == tmp_path / "garden" / "seedling"

        content = result.read_text()
        assert content.startswith("---\n")
        # The legacy ``type: idea`` field is preserved when the caller still
        # passes a note_type (1-minor back-compat shim).
        assert "type: idea" in content
        assert "maturity: seedling" in content
        assert "status: seed" in content
        assert "source: garden-writer" in content
        assert "captured_at:" in content
        assert "[[BSage]]" in content
        assert "This is an idea about something." in content

    @pytest.mark.asyncio
    async def test_write_garden_slug_from_title(self, tmp_path: Path) -> None:
        """write_garden should generate a slug from the title."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(
            title="My Great Idea",
            content="Content here.",
            note_type="idea",
            source="test",
        )
        result = await writer.write_garden(note)

        assert result.name == "my-great-idea.md"

    @pytest.mark.asyncio
    async def test_write_garden_dedup_with_timestamp_suffix(self, tmp_path: Path) -> None:
        """Writing the same slug twice should create slug.md then slug_001.md."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(
            title="Duplicate Idea",
            content="First version.",
            note_type="idea",
            source="test",
        )

        first = await writer.write_garden(note)
        assert first.name == "duplicate-idea.md"

        note2 = GardenNote(
            title="Duplicate Idea",
            content="Second version.",
            note_type="idea",
            source="test",
        )
        second = await writer.write_garden(note2)
        assert second.name == "duplicate-idea_001.md"

        note3 = GardenNote(
            title="Duplicate Idea",
            content="Third version.",
            note_type="idea",
            source="test",
        )
        third = await writer.write_garden(note3)
        assert third.name == "duplicate-idea_002.md"

    @pytest.mark.asyncio
    async def test_write_garden_special_chars_in_title(self, tmp_path: Path) -> None:
        """write_garden should handle special characters in titles."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(
            title="Hello, World! (2026)",
            content="Content.",
            note_type="idea",
            source="test",
        )
        result = await writer.write_garden(note)

        # Slug should be lowercase, hyphens, no special chars
        assert result.name == "hello-world-2026.md"

    @pytest.mark.asyncio
    async def test_write_garden_unicode_title(self, tmp_path: Path) -> None:
        """write_garden should preserve Unicode characters in slugs."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(
            title="자동화의 자동화 프로젝트",
            content="한글 내용입니다.",
            note_type="idea",
            source="test",
        )
        result = await writer.write_garden(note)

        assert result.name == "자동화의-자동화-프로젝트.md"
        assert result.exists()
        content = result.read_text()
        assert "# 자동화의 자동화 프로젝트" in content

    @pytest.mark.asyncio
    async def test_write_garden_mixed_unicode_ascii_title(self, tmp_path: Path) -> None:
        """write_garden should handle titles with both Unicode and ASCII."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(
            title="harness-studio 컴포넌트 라이브러리",
            content="Mixed content.",
            note_type="idea",
            source="test",
        )
        result = await writer.write_garden(note)

        assert result.name == "harness-studio-컴포넌트-라이브러리.md"
        assert result.exists()


class TestWriteAction:
    """Test GardenWriter.write_action appends to daily log."""

    @pytest.mark.asyncio
    async def test_write_action_appends_to_daily_log(self, tmp_path: Path) -> None:
        """write_action should append an entry with timestamp and skill name."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        await writer.write_action("garden-writer", "Processed 3 notes")

        actions_dir = tmp_path / "actions"
        md_files = list(actions_dir.glob("*.md"))
        assert len(md_files) == 1

        content = md_files[0].read_text()
        assert "garden-writer" in content
        assert "Processed 3 notes" in content

    @pytest.mark.asyncio
    async def test_write_action_creates_actions_dir_if_missing(self, tmp_path: Path) -> None:
        """write_action should handle missing actions/ directory gracefully."""
        vault = Vault(tmp_path)
        # Intentionally NOT calling ensure_dirs — actions/ doesn't exist
        writer = GardenWriter(vault)

        await writer.write_action("test-skill", "Action summary")

        actions_dir = tmp_path / "actions"
        assert actions_dir.is_dir()
        md_files = list(actions_dir.glob("*.md"))
        assert len(md_files) == 1
        content = md_files[0].read_text()
        assert "test-skill" in content

    @pytest.mark.asyncio
    async def test_write_action_appends_multiple_entries(self, tmp_path: Path) -> None:
        """Multiple write_action calls on the same day append to the same file."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        await writer.write_action("skill-a", "First action")
        await writer.write_action("skill-b", "Second action")

        actions_dir = tmp_path / "actions"
        md_files = list(actions_dir.glob("*.md"))
        assert len(md_files) == 1

        content = md_files[0].read_text()
        assert "skill-a" in content
        assert "First action" in content
        assert "skill-b" in content
        assert "Second action" in content

    @pytest.mark.asyncio
    async def test_write_action_truncates_long_summary(self, tmp_path: Path) -> None:
        """Long summaries should be truncated to _MAX_ACTION_SUMMARY chars."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        long_summary = "x" * 500
        await writer.write_action("test-skill", long_summary)

        actions_dir = tmp_path / "actions"
        content = list(actions_dir.glob("*.md"))[0].read_text()
        # The entry line should contain at most 200 x's + ellipsis, not 500
        assert "x" * 200 + "…" in content
        assert "x" * 201 not in content


class TestReadNotes:
    """Test GardenWriter.read_notes delegates to vault."""

    @pytest.mark.asyncio
    async def test_read_notes_delegates_to_vault(self, tmp_path: Path) -> None:
        """read_notes should delegate to vault's read_notes method."""
        vault = Vault(tmp_path)
        notes_dir = tmp_path / "garden" / "ideas"
        notes_dir.mkdir(parents=True)
        (notes_dir / "note-a.md").write_text("# Note A")
        (notes_dir / "note-b.md").write_text("# Note B")

        writer = GardenWriter(vault)
        result = await writer.read_notes("garden/ideas")

        assert len(result) == 2
        assert result[0].name == "note-a.md"
        assert result[1].name == "note-b.md"


def _make_sync_manager() -> tuple[SyncManager, AsyncMock]:
    """Create a SyncManager with one mock backend, return (manager, backend)."""
    mgr = SyncManager()
    backend = AsyncMock(spec=SyncBackend)
    type(backend).name = PropertyMock(return_value="test-backend")
    mgr.register(backend)
    return mgr, backend


class TestGardenWriterSync:
    """Test that GardenWriter notifies SyncManager after writes."""

    @pytest.mark.asyncio
    async def test_write_seed_notifies_sync(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        mgr, backend = _make_sync_manager()
        writer = GardenWriter(vault, sync_manager=mgr)

        await writer.write_seed("calendar", {"event": "test"})

        backend.sync.assert_called_once()
        event = backend.sync.call_args[0][0]
        assert event.event_type == WriteEventType.SEED
        assert event.source == "calendar"

    @pytest.mark.asyncio
    async def test_write_garden_notifies_sync(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        mgr, backend = _make_sync_manager()
        writer = GardenWriter(vault, sync_manager=mgr)

        note = GardenNote(
            title="Sync Test",
            content="Content.",
            note_type="idea",
            source="test-skill",
        )
        await writer.write_garden(note)

        backend.sync.assert_called_once()
        event = backend.sync.call_args[0][0]
        assert event.event_type == WriteEventType.GARDEN
        assert event.source == "test-skill"

    @pytest.mark.asyncio
    async def test_write_action_notifies_sync(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        mgr, backend = _make_sync_manager()
        writer = GardenWriter(vault, sync_manager=mgr)

        await writer.write_action("test-skill", "Did something")

        backend.sync.assert_called_once()
        event = backend.sync.call_args[0][0]
        assert event.event_type == WriteEventType.ACTION
        assert event.source == "test-skill"

    @pytest.mark.asyncio
    async def test_write_without_sync_manager(self, tmp_path: Path) -> None:
        """GardenWriter with no sync_manager should work exactly as before."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.write_seed("calendar", {"event": "test"})
        assert result.exists()

    @pytest.mark.asyncio
    async def test_write_garden_dict_notifies_sync(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        mgr, backend = _make_sync_manager()
        writer = GardenWriter(vault, sync_manager=mgr)

        await writer.write_garden(
            {
                "title": "Dict Note",
                "content": "From dict.",
                "note_type": "idea",
                "source": "dict-source",
            }
        )

        backend.sync.assert_called_once()
        event = backend.sync.call_args[0][0]
        assert event.source == "dict-source"


class TestHandleWriteNote:
    """Test GardenWriter.handle_write_note — LLM tool handler."""

    @pytest.mark.asyncio
    async def test_handle_write_note_calls_write_garden(self, tmp_path: Path) -> None:
        """handle_write_note should write a garden note and return result."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_note(
            {"title": "Test Note", "content": "Body text", "tags": ["demo"]}
        )

        assert result["status"] == "saved"
        assert result["title"] == "Test Note"
        # Post dynamic-ontology refactor: handle_write_note no longer
        # echoes a note_type — identity is carried by tags + entities.
        assert "note_type" not in result
        assert "path" in result
        assert Path(result["path"]).exists()

    @pytest.mark.asyncio
    async def test_write_garden_evergreen_lands_in_evergreen_folder(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        path = await writer.write_garden(
            GardenNote(
                title="Stable Idea",
                content="...",
                source="manual",
                maturity="evergreen",
            )
        )
        assert path.parent == tmp_path / "garden" / "evergreen"
        assert "maturity: evergreen" in path.read_text()

    @pytest.mark.asyncio
    async def test_write_garden_unknown_maturity_falls_back_to_seedling(
        self, tmp_path: Path
    ) -> None:
        # Typo / out-of-band value must not strand the note in
        # ``garden/banana`` — fall back to seedling so it stays linkable.
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        path = await writer.write_garden(
            GardenNote(
                title="Typo",
                content="...",
                source="manual",
                maturity="banana",
            )
        )
        assert path.parent == tmp_path / "garden" / "seedling"

    @pytest.mark.asyncio
    async def test_handle_write_note_lands_in_seedling_folder(self, tmp_path: Path) -> None:
        """Without note_type, new notes land in garden/seedling/ — the
        first stage of the Andy Matuschak growth cycle."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_note({"title": "Minimal", "content": "Body"})

        path = Path(result["path"])
        assert path.parent.name == "seedling"
        assert path.parent.parent.name == "garden"
        # Frontmatter carries maturity but not the legacy "type:" field.
        body = path.read_text()
        assert "maturity: seedling" in body
        assert "type: idea" not in body

    @pytest.mark.asyncio
    async def test_handle_write_note_sets_source_to_chat(self, tmp_path: Path) -> None:
        """Source should always be 'chat'."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_note({"title": "Source Test", "content": "Body"})

        content = Path(result["path"]).read_text()
        assert "source: chat" in content

    @pytest.mark.asyncio
    async def test_handle_write_note_empty_args(self, tmp_path: Path) -> None:
        """Empty args should produce an 'Untitled' note."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_note({})

        assert result["title"] == "Untitled"
        assert result["status"] == "saved"


class TestHandleWriteSeed:
    """Test GardenWriter.handle_write_seed — LLM tool handler."""

    @pytest.mark.asyncio
    async def test_handle_write_seed_creates_seed(self, tmp_path: Path) -> None:
        """handle_write_seed should write a seed with title/content."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_seed({"title": "My Idea", "content": "Some raw thought"})

        assert result["status"] == "saved"
        assert result["title"] == "My Idea"
        assert "path" in result
        path = Path(result["path"])
        assert path.exists()
        text = path.read_text()
        assert "type: seed" in text
        assert "source: idea" in text
        assert "My Idea" in text

    @pytest.mark.asyncio
    async def test_handle_write_seed_saves_to_idea_source(self, tmp_path: Path) -> None:
        """Source should always be 'idea' for LLM tool calls."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_seed({"title": "Test", "content": "Body"})

        path = Path(result["path"])
        assert "seeds/idea" in str(path)

    @pytest.mark.asyncio
    async def test_handle_write_seed_includes_tags(self, tmp_path: Path) -> None:
        """Tags should be included in the seed data."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        result = await writer.handle_write_seed(
            {"title": "Tagged", "content": "Body", "tags": ["ai", "tool"]}
        )

        path = Path(result["path"])
        text = path.read_text()
        assert "ai" in text
        assert "tool" in text


class TestGardenWriterEvents:
    """Test EventBus emission from GardenWriter."""

    async def test_write_seed_emits_seed_written(self, tmp_path: Path) -> None:
        from backend.knowledge._internal.events import EventBus, EventType

        event_bus = EventBus()
        sub = AsyncMock()
        event_bus.subscribe(sub)

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault, event_bus=event_bus)
        await writer.write_seed("test-source", {"data": "hello"})

        events = [c.args[0] for c in sub.on_event.call_args_list]
        assert any(e.event_type == EventType.SEED_WRITTEN for e in events)

    async def test_write_garden_emits_garden_written(self, tmp_path: Path) -> None:
        from backend.knowledge._internal.events import EventBus, EventType

        event_bus = EventBus()
        sub = AsyncMock()
        event_bus.subscribe(sub)

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault, event_bus=event_bus)
        await writer.write_garden(
            {"title": "Test Note", "content": "body", "note_type": "idea", "source": "test"}
        )

        events = [c.args[0] for c in sub.on_event.call_args_list]
        assert any(e.event_type == EventType.GARDEN_WRITTEN for e in events)

    async def test_write_action_emits_action_logged(self, tmp_path: Path) -> None:
        from backend.knowledge._internal.events import EventBus, EventType

        event_bus = EventBus()
        sub = AsyncMock()
        event_bus.subscribe(sub)

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault, event_bus=event_bus)
        await writer.write_action("test-skill", "did something")

        events = [c.args[0] for c in sub.on_event.call_args_list]
        assert any(e.event_type == EventType.ACTION_LOGGED for e in events)

    async def test_no_events_when_event_bus_is_none(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)  # no event_bus
        path = await writer.write_seed("src", {"x": 1})
        assert path.exists()


class TestUpdateNote:
    """Test GardenWriter.update_note — replace note content."""

    @pytest.mark.asyncio
    async def test_update_note_preserves_frontmatter(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Original", content="Old body.", note_type="idea", source="test")
        original_path = await writer.write_garden(note)
        rel_path = str(original_path.relative_to(tmp_path))

        result = await writer.update_note(rel_path, "New body.", preserve_frontmatter=True)
        content = result.read_text()
        assert "type: idea" in content
        assert "source: test" in content
        assert "New body." in content
        assert "Old body." not in content

    @pytest.mark.asyncio
    async def test_update_note_replaces_all_when_no_preserve(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Original", content="Old body.", note_type="idea", source="test")
        original_path = await writer.write_garden(note)
        rel_path = str(original_path.relative_to(tmp_path))

        await writer.update_note(rel_path, "Completely new.", preserve_frontmatter=False)
        content = original_path.read_text()
        assert content == "Completely new."

    @pytest.mark.asyncio
    async def test_update_note_raises_on_missing_file(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        with pytest.raises(FileNotFoundError):
            await writer.update_note("garden/idea/nonexistent.md", "content")

    @pytest.mark.asyncio
    async def test_update_note_emits_event(self, tmp_path: Path) -> None:
        from backend.knowledge._internal.events import EventBus, EventType

        event_bus = EventBus()
        sub = AsyncMock()
        event_bus.subscribe(sub)

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault, event_bus=event_bus)

        note = GardenNote(title="Event Test", content="Body.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        sub.on_event.reset_mock()

        rel_path = str(path.relative_to(tmp_path))
        await writer.update_note(rel_path, "Updated body.")

        events = [c.args[0] for c in sub.on_event.call_args_list]
        assert any(e.event_type == EventType.NOTE_UPDATED for e in events)

    @pytest.mark.asyncio
    async def test_update_note_preserve_raises_on_malformed_frontmatter(
        self, tmp_path: Path
    ) -> None:
        """Earlier behaviour silently dropped existing frontmatter when the
        closing ``---`` was missing — caller thought metadata was preserved
        while title/status/related disappeared. Now we raise so the caller
        knows the on-disk state is corrupt."""
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        # Hand-craft a malformed file: opening fence, no closing fence.
        broken = vault.root / "garden" / "idea" / "broken.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("---\ntitle: Broken\nstatus: active\n\nbody only", encoding="utf-8")

        with pytest.raises(ValueError, match="no closing '---' found"):
            await writer.update_note("garden/idea/broken.md", "new body", preserve_frontmatter=True)
        # File untouched
        assert broken.read_text() == "---\ntitle: Broken\nstatus: active\n\nbody only"


class TestSetFrontmatterField:
    """Regression: silent no-ops on `_set_frontmatter_field` masked failed
    maturity promotions and other status flips. The caller MUST observe
    success vs failure of its mutation."""

    @pytest.mark.asyncio
    async def test_injects_frontmatter_when_note_has_none(self, tmp_path: Path) -> None:
        from backend.knowledge.graph.markdown_utils import extract_frontmatter

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        plain = vault.root / "garden" / "idea" / "plain.md"
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text("# Plain note\n\nBody.\n", encoding="utf-8")

        await writer._set_frontmatter_field(plain, "maturity", "evergreen")
        fm = extract_frontmatter(plain.read_text(encoding="utf-8"))
        assert fm["maturity"] == "evergreen"

    @pytest.mark.asyncio
    async def test_raises_on_missing_closing_fence(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        broken = vault.root / "garden" / "idea" / "broken.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("---\nfoo: bar\n\nbody only", encoding="utf-8")

        with pytest.raises(ValueError, match="no closing '---' found"):
            await writer._set_frontmatter_field(broken, "maturity", "budding")

    @pytest.mark.asyncio
    async def test_raises_on_corrupted_yaml(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        broken = vault.root / "garden" / "idea" / "yamlbroken.md"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("---\nfoo: [unclosed\n---\n\nbody\n", encoding="utf-8")

        with pytest.raises(ValueError, match="corrupted YAML"):
            await writer._set_frontmatter_field(broken, "maturity", "budding")


class TestAppendToNote:
    """Test GardenWriter.append_to_note."""

    @pytest.mark.asyncio
    async def test_append_to_note_adds_text(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Append Test", content="Original.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        rel_path = str(path.relative_to(tmp_path))

        await writer.append_to_note(rel_path, "\n\nAppended text.")
        content = path.read_text()
        assert "Original." in content
        assert "Appended text." in content

    @pytest.mark.asyncio
    async def test_append_to_note_raises_on_missing_file(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        with pytest.raises(FileNotFoundError):
            await writer.append_to_note("garden/idea/nonexistent.md", "text")


class TestDeleteNote:
    """Test GardenWriter.delete_note."""

    @pytest.mark.asyncio
    async def test_delete_note_removes_file(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Delete Me", content="Body.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        rel_path = str(path.relative_to(tmp_path))
        assert path.exists()

        await writer.delete_note(rel_path)
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_delete_note_rejects_actions(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        with pytest.raises(ValueError, match="Cannot delete action logs"):
            await writer.delete_note("actions/2026-03-07.md")

    @pytest.mark.asyncio
    async def test_delete_note_raises_on_missing_file(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        with pytest.raises(FileNotFoundError):
            await writer.delete_note("garden/idea/nonexistent.md")

    @pytest.mark.asyncio
    async def test_delete_note_emits_event(self, tmp_path: Path) -> None:
        from backend.knowledge._internal.events import EventBus, EventType

        event_bus = EventBus()
        sub = AsyncMock()
        event_bus.subscribe(sub)

        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault, event_bus=event_bus)

        note = GardenNote(title="Delete Event", content="Body.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        sub.on_event.reset_mock()

        rel_path = str(path.relative_to(tmp_path))
        await writer.delete_note(rel_path)

        events = [c.args[0] for c in sub.on_event.call_args_list]
        assert any(e.event_type == EventType.NOTE_DELETED for e in events)


class TestHandleUpdateNote:
    """Test GardenWriter.handle_update_note — LLM tool handler."""

    @pytest.mark.asyncio
    async def test_handle_update_note_returns_result(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Handler Test", content="Body.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        rel_path = str(path.relative_to(tmp_path))

        result = await writer.handle_update_note({"path": rel_path, "content": "Updated body."})
        assert result["status"] == "updated"
        assert "path" in result


class TestHandleAppendNote:
    """Test GardenWriter.handle_append_note — LLM tool handler."""

    @pytest.mark.asyncio
    async def test_handle_append_note_returns_result(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Append Handler", content="Body.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        rel_path = str(path.relative_to(tmp_path))

        result = await writer.handle_append_note({"path": rel_path, "text": "\n\nExtra."})
        assert result["status"] == "appended"
        assert "path" in result
        content = path.read_text()
        assert "Extra." in content

    @pytest.mark.asyncio
    async def test_handle_append_note_raises_on_missing(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        with pytest.raises(FileNotFoundError):
            await writer.handle_append_note({"path": "garden/idea/no.md", "text": "x"})


class TestHandleDeleteNote:
    """Test GardenWriter.handle_delete_note — LLM tool handler."""

    @pytest.mark.asyncio
    async def test_handle_delete_note_returns_result(self, tmp_path: Path) -> None:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        writer = GardenWriter(vault)

        note = GardenNote(title="Handler Delete", content="Body.", note_type="idea", source="test")
        path = await writer.write_garden(note)
        rel_path = str(path.relative_to(tmp_path))

        result = await writer.handle_delete_note({"path": rel_path})
        assert result["status"] == "deleted"
        assert not path.exists()
