"""SkillLoader — frontmatter parsing, dropped-field rejection, registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.extensions.skill.exceptions import SkillLoadError
from backend.extensions.skill.loader import SkillLoader


def _write(path: Path, name: str, body: str) -> Path:
    p = path / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_minimal_skill(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "weekly-digest",
        "---\nname: weekly-digest\nversion: 1.0.0\ndescription: A digest.\n---\n\nDo it.\n",
    )
    loader = SkillLoader(tmp_path)
    reg = loader.load_all()
    assert set(reg.keys()) == {"weekly-digest"}
    meta = loader.get("weekly-digest")
    assert meta.system_prompt == "Do it."


def test_optional_fields_loaded(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "x",
        (
            "---\n"
            "name: x\nversion: 1.0\ndescription: y\n"
            "author: ada\n"
            "allowed_tools: [search_knowledge, create_note]\n"
            "model: openai/gpt-4o\n"
            "---\nbody\n"
        ),
    )
    loader = SkillLoader(tmp_path)
    loader.load_all()
    m = loader.get("x")
    assert m.author == "ada"
    assert m.allowed_tools == ["search_knowledge", "create_note"]
    assert m.model == "openai/gpt-4o"


@pytest.mark.parametrize(
    "dropped",
    [
        "category: process",
        "trigger:\n  type: cron",
        "read_context:\n  - garden/idea",
        "output_target: garden",
        "output_format: json",
        "credentials:\n  - name: api_key",
    ],
)
def test_rejects_dropped_fields(tmp_path: Path, dropped: str) -> None:
    _write(
        tmp_path,
        "bad",
        f"---\nname: bad\nversion: 1\ndescription: d\n{dropped}\n---\nbody",
    )
    loader = SkillLoader(tmp_path)
    loader.load_all()
    # Loader logs + skips bad files. Verify the registry skipped it.
    assert "bad" not in loader.registry


def test_explicit_parse_raises_on_dropped_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "bad",
        "---\nname: bad\nversion: 1\ndescription: d\ncategory: process\n---\nbody",
    )
    with pytest.raises(SkillLoadError, match="removed frontmatter fields"):
        SkillLoader._parse(path)


def test_missing_required_field(tmp_path: Path) -> None:
    path = _write(tmp_path, "noversion", "---\nname: x\ndescription: d\n---\nbody")
    with pytest.raises(SkillLoadError, match="missing required fields"):
        SkillLoader._parse(path)


def test_unknown_keys_silently_dropped(tmp_path: Path) -> None:
    # Forward-compat: an unknown key (not in ALLOWED, not in DROPPED) is ignored.
    _write(
        tmp_path,
        "x",
        "---\nname: x\nversion: 1\ndescription: d\nfuture_field: stuff\n---\nbody",
    )
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert "x" in loader.registry


def test_missing_dir_returns_empty_registry(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path / "nonexistent")
    assert loader.load_all() == {}


def test_no_frontmatter_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "plain", "no frontmatter just body")
    loader = SkillLoader(tmp_path)
    loader.load_all()
    assert loader.registry == {}


def test_invalid_name_in_frontmatter(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "x",
        "---\nname: BadName\nversion: 1\ndescription: d\n---\nbody",
    )
    with pytest.raises(SkillLoadError, match="Invalid skill name"):
        SkillLoader._parse(path)


def test_get_raises_when_missing(tmp_path: Path) -> None:
    loader = SkillLoader(tmp_path)
    with pytest.raises(SkillLoadError, match="not in registry"):
        loader.get("nonexistent")
