"""Import-root confinement for the Obsidian import — uses the REAL ``SkillContext``."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from backend.extensions.plugin import PluginRunError, PluginRunner
from backend.extensions.plugin.context import SkillContext
from plugin.obsidian import plugin as obsidian_module
from plugin.obsidian.client import VaultScanner

P = obsidian_module.p


class _Knowledge:
    def __init__(self) -> None:
        self.titles: list[str] = []

    async def write_seed(self, source: str, data: dict[str, Any]) -> Path:
        self.titles.append(data["title"])
        return Path("/seed")


class _Llm:
    async def chat(self, *a: Any, **k: Any) -> str:
        raise RuntimeError


def _ctx(knowledge: _Knowledge, import_root: Path | None, **kw: Any) -> SkillContext:
    return SkillContext(
        llm=_Llm(),
        config=kw.pop("config", {}),
        logger=None,
        knowledge=knowledge,
        import_root=str(import_root) if import_root else None,
        **kw,
    )


async def _run(ctx: SkillContext, **kwargs: Any) -> dict[str, Any]:
    return await PluginRunner().dispatch_action(
        P.meta, action_name="import_vault", context=ctx, kwargs=kwargs
    )


def _md(root: Path, rel: str, body: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "imports" / "ws"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    _md(outside, "secret.md", "secret")
    return root, outside


@pytest.mark.asyncio
async def test_absolute_path_outside_root_rejected(layout):
    root, outside = layout
    k = _Knowledge()
    with pytest.raises(PluginRunError, match="outside the allowed import root"):
        await _run(_ctx(k, root), vault_path=str(outside))
    assert k.titles == []


@pytest.mark.asyncio
async def test_dotdot_rejected(layout):
    root, _ = layout
    with pytest.raises(PluginRunError, match="outside"):
        await _run(_ctx(_Knowledge(), root), vault_path=str(root / ".." / "outside"))


@pytest.mark.asyncio
async def test_symlinked_vault_dir_pointing_outside_rejected(layout):
    root, outside = layout
    (root / "link").symlink_to(outside)
    with pytest.raises(PluginRunError, match="outside"):
        await _run(_ctx(_Knowledge(), root), vault_path=str(root / "link"))


@pytest.mark.asyncio
async def test_allowed_path_imported(layout):
    root, _ = layout
    _md(root, "vault/a.md", "a")
    k = _Knowledge()
    result = await _run(_ctx(k, root), vault_path=str(root / "vault"))
    assert result["notes_count"] == 1
    # relative path is resolved under the root
    result = await _run(_ctx(_Knowledge(), root), vault_path="vault")
    assert result["notes_count"] == 1


@pytest.mark.asyncio
async def test_no_import_root_rejected(layout):
    root, _ = layout
    _md(root, "a.md")
    with pytest.raises(PluginRunError, match="import root"):
        await _run(_ctx(_Knowledge(), None), vault_path=str(root))


@pytest.mark.asyncio
async def test_relative_import_root_rejected(layout):
    ctx = _ctx(_Knowledge(), None)
    ctx.import_root = "relative/dir"
    with pytest.raises(PluginRunError, match="import root"):
        await _run(ctx, vault_path="x")


@pytest.mark.asyncio
async def test_symlink_file_outside_skipped(layout):
    root, outside = layout
    _md(root, "ok.md")
    (root / "leak.md").symlink_to(outside / "secret.md")
    k = _Knowledge()
    result = await _run(_ctx(k, root), vault_path=str(root))
    assert k.titles == ["ok"]
    assert result["skipped_outside_root"] == 1


@pytest.mark.asyncio
async def test_symlink_dir_outside_skipped(layout):
    root, outside = layout
    _md(root, "ok.md")
    (root / "dirlink").symlink_to(outside, target_is_directory=True)
    k = _Knowledge()
    result = await _run(_ctx(k, root), vault_path=str(root))
    assert k.titles == ["ok"]
    assert result["skipped_outside_root"] == 1


def test_symlink_loop_is_not_followed(layout):
    root, _ = layout
    _md(root, "ok.md")
    (root / "loop").symlink_to(root, target_is_directory=True)
    scanner = VaultScanner(root, allowed_root=root)
    assert [n.relative_path for n in scanner.scan()] == ["ok.md"]
    assert scanner.skipped_symlinks == 1


def test_file_swapped_for_symlink_is_not_opened(layout):
    root, outside = layout
    link = root / "x.md"
    link.symlink_to(outside / "secret.md")
    scanner = VaultScanner(root, allowed_root=root)
    assert scanner._read(link) is None  # O_NOFOLLOW refuses


def test_note_cap_stops_with_reason(layout):
    root, _ = layout
    for i in range(5):
        _md(root, f"n{i}.md")
    scanner = VaultScanner(root, allowed_root=root, max_notes=3)
    assert len(list(scanner.scan())) == 3
    assert "note limit" in (scanner.stop_reason or "")


def test_entry_cap_counts_non_md_entries(layout):
    root, _ = layout
    for i in range(10):
        _md(root, f"f{i}.txt")
    scanner = VaultScanner(root, allowed_root=root, max_entries=4)
    assert list(scanner.scan()) == []
    assert "entry limit" in (scanner.stop_reason or "")


def test_byte_cap_counts_bytes_actually_read(layout):
    root, _ = layout
    _md(root, "a.md", "x" * 50)
    _md(root, "b.md", "y" * 50)
    scanner = VaultScanner(root, allowed_root=root, max_bytes=60)
    assert [n.relative_path for n in scanner.scan()] == ["a.md"]
    assert "byte limit" in (scanner.stop_reason or "")


def test_excluded_dirs_are_not_descended(layout, monkeypatch):
    root, _ = layout
    _md(root, ".obsidian/p/deep.md")
    _md(root, "ok.md")
    seen: list[str] = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: (seen.append(str(p)), real(p))[1])
    assert [n.relative_path for n in VaultScanner(root, allowed_root=root).scan()] == ["ok.md"]
    assert not any(".obsidian" in s for s in seen)


@pytest.mark.asyncio
async def test_result_reports_stop_reason(layout):
    root, _ = layout
    _md(root, "a.md")
    result = await _run(_ctx(_Knowledge(), root), vault_path=str(root))
    assert "stopped" not in result
