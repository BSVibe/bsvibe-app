"""Tests for backend.knowledge.graph.vault_linter — unified vault health check."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.vault_linter import VaultLinter
from backend.knowledge.graph.writer import GardenNote, GardenWriter


class TestVaultLinter:
    """Test VaultLinter lint checks."""

    @pytest.fixture()
    def vault_and_writer(self, tmp_path: Path) -> tuple[Vault, GardenWriter]:
        vault = Vault(tmp_path)
        vault.ensure_dirs()
        return vault, GardenWriter(vault)

    @pytest.fixture()
    def mock_graph_store(self) -> AsyncMock:
        store = AsyncMock()
        store.search_entities = AsyncMock(return_value=[])
        store.query_neighbors = AsyncMock(return_value=[])
        return store

    @pytest.fixture()
    def linter(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_graph_store: AsyncMock,
    ) -> Any:
        vault, writer = vault_and_writer
        return VaultLinter(vault=vault, garden_writer=writer, graph_store=mock_graph_store)

    @pytest.mark.asyncio
    async def test_lint_returns_report(self, linter: Any) -> None:
        """lint() should return a LintReport dataclass."""
        from backend.knowledge.graph.vault_linter import LintReport

        report = await linter.lint()
        assert isinstance(report, LintReport)
        assert isinstance(report.issues, list)
        assert isinstance(report.total_notes_scanned, int)

    @pytest.mark.asyncio
    async def test_lint_detects_orphan_notes(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_graph_store: AsyncMock,
    ) -> None:
        """Notes with no related links (incoming or outgoing) should be flagged as orphans."""

        vault, writer = vault_and_writer
        # Create a note with no related links
        await writer.write_garden(
            GardenNote(
                title="Lonely Note",
                content="No connections.",
                note_type="idea",
                source="test",
            )
        )
        linter = VaultLinter(vault=vault, garden_writer=writer, graph_store=mock_graph_store)
        report = await linter.lint()

        orphan_issues = [i for i in report.issues if i.check == "orphan"]
        assert len(orphan_issues) >= 1
        assert "Lonely Note" in orphan_issues[0].description

    @pytest.mark.asyncio
    async def test_lint_no_orphan_when_related(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_graph_store: AsyncMock,
    ) -> None:
        """Notes with related links should NOT be flagged as orphans."""

        vault, writer = vault_and_writer
        await writer.write_garden(
            GardenNote(
                title="Connected Note",
                content="Has connections.",
                note_type="idea",
                source="test",
                related=["Other Note"],
            )
        )
        linter = VaultLinter(vault=vault, garden_writer=writer, graph_store=mock_graph_store)
        report = await linter.lint()

        orphan_issues = [i for i in report.issues if i.check == "orphan"]
        connected = [i for i in orphan_issues if "Connected Note" in i.description]
        assert len(connected) == 0

    @pytest.mark.asyncio
    async def test_lint_detects_stale_notes(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_graph_store: AsyncMock,
    ) -> None:
        """Notes with very old captured_at should be flagged as stale."""

        vault, writer = vault_and_writer
        # Create a note manually with old date in the maturity-based layout.
        note_dir = vault.root / "garden" / "seedling"
        note_dir.mkdir(parents=True, exist_ok=True)
        old_note = note_dir / "old-note.md"
        old_content = (
            "---\nmaturity: seedling\nstatus: seed\nsource: test\n"
            "captured_at: 2020-01-01\n---\n\n# Old Note\n\nVery old."
        )
        old_note.write_text(old_content, encoding="utf-8")

        linter = VaultLinter(
            vault=vault, garden_writer=writer, graph_store=mock_graph_store, stale_days=30
        )
        report = await linter.lint()

        stale_issues = [i for i in report.issues if i.check == "stale"]
        assert len(stale_issues) >= 1

    @pytest.mark.asyncio
    async def test_lint_writes_report_note(
        self,
        vault_and_writer: tuple[Vault, GardenWriter],
        mock_graph_store: AsyncMock,
    ) -> None:
        """lint() should write a lint report as a garden insight note."""

        vault, writer = vault_and_writer
        await writer.write_garden(
            GardenNote(title="Test", content="test", note_type="idea", source="test")
        )
        linter = VaultLinter(vault=vault, garden_writer=writer, graph_store=mock_graph_store)
        await linter.lint()

        # Check that a lint report note was written. After the dynamic
        # ontology refactor lint reports go to garden/seedling like any
        # other write — Step B5 will likely promote the linter to write
        # them straight to evergreen.
        report_files = list((vault.root / "garden" / "seedling").glob("vault-lint-*.md"))
        assert len(report_files) >= 1
        content = report_files[0].read_text()
        assert "Vault Lint Report" in content

    @pytest.mark.asyncio
    async def test_lint_empty_vault(self, linter: Any) -> None:
        """lint() on empty vault should return empty report without errors."""
        report = await linter.lint()
        assert report.total_notes_scanned == 0
        assert report.issues == []
