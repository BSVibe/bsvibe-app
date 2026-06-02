"""VaultScanner unit tests.

Builds tiny fake Obsidian vaults under ``tmp_path`` and asserts the scanner's
filtering + relative-path discipline. No real Obsidian files; no LLM; no
network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plugin.obsidian.client import ScannedNote, VaultScanner


def _write(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestVaultScanner:
    def test_scan_returns_notes_with_relative_path_and_text(self, tmp_path):
        _write(tmp_path, "a.md", "# a")
        _write(tmp_path, "sub/b.md", "# b")
        scanner = VaultScanner(tmp_path, exclude_patterns=[])
        notes = list(scanner.scan())
        rels = sorted(n.relative_path for n in notes)
        assert rels == ["a.md", "sub/b.md"]
        assert all(isinstance(n, ScannedNote) for n in notes)
        assert all(n.text for n in notes)

    def test_scan_excludes_obsidian_config_by_default(self, tmp_path):
        _write(tmp_path, ".obsidian/workspace.md", "config")
        _write(tmp_path, "real.md", "# real")
        scanner = VaultScanner(tmp_path)  # uses default exclude list
        rels = [n.relative_path for n in scanner.scan()]
        assert rels == ["real.md"]

    def test_scan_excludes_templates_by_default(self, tmp_path):
        _write(tmp_path, "Templates/daily.md", "{{date}}")
        _write(tmp_path, "Projects/foo.md", "# foo")
        scanner = VaultScanner(tmp_path)
        rels = [n.relative_path for n in scanner.scan()]
        assert rels == ["Projects/foo.md"]

    def test_custom_exclude_patterns_take_priority(self, tmp_path):
        _write(tmp_path, "Templates/keep.md", "kept!")
        _write(tmp_path, "Drafts/skip.md", "draft")
        # Caller supplies a list — replaces defaults entirely.
        scanner = VaultScanner(tmp_path, exclude_patterns=["Drafts/**"])
        rels = sorted(n.relative_path for n in scanner.scan())
        assert rels == ["Templates/keep.md"]

    def test_skips_non_markdown(self, tmp_path):
        _write(tmp_path, "note.md", "# md")
        _write(tmp_path, "image.png", "junk")
        _write(tmp_path, "notes.txt", "plain")
        scanner = VaultScanner(tmp_path, exclude_patterns=[])
        rels = [n.relative_path for n in scanner.scan()]
        assert rels == ["note.md"]

    def test_six_realistic_vault_filters_to_three(self, tmp_path):
        # The exact "5 files, 3 valid" delta from the lift spec, slightly
        # expanded so the templates/.obsidian filters both fire.
        _write(tmp_path, "Inbox/today.md", "# today")
        _write(tmp_path, "Projects/foo/bar.md", "# bar")
        _write(tmp_path, "Projects/foo/baz.md", "# baz")
        _write(tmp_path, "Templates/daily.md", "tpl")
        _write(tmp_path, ".obsidian/plugins.md", "cfg")
        scanner = VaultScanner(tmp_path)
        notes = list(scanner.scan())
        assert len(notes) == 3
        # Subdirectory paths preserve their relative location.
        assert any(n.relative_path == "Projects/foo/bar.md" for n in notes)

    def test_missing_vault_root_raises(self, tmp_path):
        scanner = VaultScanner(tmp_path / "nope")
        with pytest.raises(FileNotFoundError):
            list(scanner.scan())

    def test_vault_root_is_file_raises(self, tmp_path):
        f = tmp_path / "vault.md"
        f.write_text("not a directory")
        scanner = VaultScanner(f)
        with pytest.raises(NotADirectoryError):
            list(scanner.scan())

    def test_default_exclude_patterns_constant_exposed(self):
        from plugin.obsidian.client import DEFAULT_EXCLUDE_PATTERNS

        assert ".obsidian/**" in DEFAULT_EXCLUDE_PATTERNS
        assert "Templates/**" in DEFAULT_EXCLUDE_PATTERNS
