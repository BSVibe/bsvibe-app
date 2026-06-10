"""Tests for backend.products.application.bootstrap.bootstrap_filter.

Lift E20 Phase A — the standard packer filter that excludes lockfiles, vendor
dirs, binaries, IDE cruft, and oversized files BEFORE the walk yields a
:class:`WalkedFile`. Replaces the implicit "feed every walked file to the LLM"
behaviour that exploded into 12,199 notes on the bsvibe-app dogfood.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.products.application.bootstrap.bootstrap_filter import (
    BootstrapFileFilter,
    FilterReason,
)


def _w(rel: str, size: int = 100) -> dict:
    return {"rel_path": rel, "size": size}


class TestBootstrapFileFilter:
    """The filter exposes a single decide() entry point and reason counters."""

    def test_drops_lockfiles(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"))
        assert f.decide("package-lock.json", size=10) is FilterReason.LOCKFILE
        assert f.decide("backend/uv.lock", size=10) is FilterReason.LOCKFILE
        assert f.decide("pnpm-lock.yaml", size=10) is FilterReason.LOCKFILE
        assert f.decide("Cargo.lock", size=10) is FilterReason.LOCKFILE
        assert f.decide("Gemfile.lock", size=10) is FilterReason.LOCKFILE
        assert f.decide("backend/extra.lock", size=10) is FilterReason.LOCKFILE
        assert f.decide("ui/icon.lockb", size=10) is FilterReason.LOCKFILE

    def test_drops_vendor_dirs(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"))
        assert f.decide("node_modules/foo/index.js", size=10) is FilterReason.VENDOR_DIR
        assert f.decide("apps/web/.next/cache/x", size=10) is FilterReason.VENDOR_DIR
        assert f.decide(".venv/lib/foo.py", size=10) is FilterReason.VENDOR_DIR
        assert f.decide("target/debug/x.o", size=10) is FilterReason.VENDOR_DIR
        assert f.decide("a/b/__pycache__/c.pyc", size=10) is FilterReason.VENDOR_DIR
        assert f.decide("vendor/foo.go", size=10) is FilterReason.VENDOR_DIR

    def test_drops_binary_extensions(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"))
        assert f.decide("logo.png", size=10) is FilterReason.BINARY_EXTENSION
        assert f.decide("assets/bg.JPG", size=10) is FilterReason.BINARY_EXTENSION
        assert f.decide("docs/spec.pdf", size=10) is FilterReason.BINARY_EXTENSION
        assert f.decide("ui/clip.mp4", size=10) is FilterReason.BINARY_EXTENSION
        assert f.decide("backend/extensions/x.so", size=10) is FilterReason.BINARY_EXTENSION

    def test_drops_ide_cruft(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"))
        assert f.decide(".idea/workspace.xml", size=10) is FilterReason.IDE_CRUFT
        assert f.decide(".vscode/settings.json", size=10) is FilterReason.IDE_CRUFT
        assert f.decide(".DS_Store", size=10) is FilterReason.IDE_CRUFT
        assert f.decide("docs/.DS_Store", size=10) is FilterReason.IDE_CRUFT
        assert f.decide("Thumbs.db", size=10) is FilterReason.IDE_CRUFT

    def test_drops_oversize(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"), max_file_bytes=50 * 1024)
        assert f.decide("ok.py", size=10) is None
        assert f.decide("big.py", size=51 * 1024) is FilterReason.OVERSIZE

    def test_keeps_source_files(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"))
        assert f.decide("backend/api.py", size=1000) is None
        assert f.decide("apps/pwa/src/App.tsx", size=1000) is None
        assert f.decide("README.md", size=200) is None

    def test_respects_gitignore_when_present(self, tmp_path: Path) -> None:
        # ``build/`` would be caught by VENDOR_DIR before .gitignore runs;
        # use a project-specific gitignore pattern instead so the
        # assertion isolates the .gitignore branch.
        (tmp_path / ".gitignore").write_text("*.log\ngenerated/\nsecret_*.txt\n")
        f = BootstrapFileFilter(repo_root=tmp_path)
        assert f.decide("a.py", size=10) is None
        assert f.decide("output.log", size=10) is FilterReason.GITIGNORE
        assert f.decide("generated/out.txt", size=10) is FilterReason.GITIGNORE
        assert f.decide("secret_key.txt", size=10) is FilterReason.GITIGNORE

    def test_counter_aggregates_reasons(self) -> None:
        f = BootstrapFileFilter(repo_root=Path("/tmp/r"))
        f.decide("package-lock.json", size=10)
        f.decide("yarn.lock", size=10)
        f.decide("node_modules/x.js", size=10)
        f.decide(".DS_Store", size=10)
        f.decide("api.py", size=10)  # kept
        summary = f.summary()
        assert summary["lockfile"] == 2
        assert summary["vendor_dir"] == 1
        assert summary["ide_cruft"] == 1
        # ``kept`` is NOT in summary; only drop reasons land there.
        assert "kept" not in summary

    def test_no_gitignore_does_not_crash(self, tmp_path: Path) -> None:
        f = BootstrapFileFilter(repo_root=tmp_path)
        assert f.decide("anything.py", size=10) is None


class TestWalkRepoFiltered:
    """walk_repo accepts a filter and drops files via filter.decide()."""

    def test_walker_uses_filter_to_drop_lockfiles(self, tmp_path: Path) -> None:
        # Tree:
        #   src/app.py       (kept)
        #   package-lock.json (filter dropped — lockfile)
        #   img/logo.png     (filter dropped — binary)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_bytes(b"x=1\n")
        (tmp_path / "package-lock.json").write_bytes(b'{"name":"x"}\n')
        (tmp_path / "img").mkdir()
        (tmp_path / "img" / "logo.png").write_bytes(b"\x89PNG fake\n")

        from backend.products.application.bootstrap.walker import walk_repo

        f = BootstrapFileFilter(repo_root=tmp_path)
        rels = sorted(w.rel_path for w in walk_repo(tmp_path, file_filter=f))
        assert rels == ["src/app.py"]
        summary = f.summary()
        assert summary.get("lockfile") == 1
        assert summary.get("binary_extension") == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
