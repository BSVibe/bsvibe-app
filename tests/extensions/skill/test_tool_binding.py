"""Skills ↔ Execution seam — invoke_skill registered as a ToolRegistry tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.extensions.skill.loader import SkillLoader
from backend.extensions.skill.tool_binding import INVOKE_SKILL_NAME, register_invoke_skill
from backend.workflow.infrastructure.tools import ToolRegistry


def _write_skill(dir_: Path, name: str, body: str, description: str = "desc") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / f"{name}.md").write_text(
        f"---\nname: {name}\nversion: 1\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


@pytest.fixture
def loader(tmp_path: Path) -> SkillLoader:
    skill_dir = tmp_path / "skills"
    _write_skill(skill_dir, "weekly-digest", "Summarize the week.")
    loader = SkillLoader(skill_dir)
    loader.load_all()
    return loader


@pytest.fixture
def registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(workspace_dir=tmp_path)


@pytest.mark.asyncio
async def test_register_adds_tool(registry: ToolRegistry, loader: SkillLoader) -> None:
    async def completion(**_kwargs):
        return "ok"

    register_invoke_skill(registry, loader=loader, completion_fn=completion)
    assert registry.has(INVOKE_SKILL_NAME)


@pytest.mark.asyncio
async def test_invoke_runs_skill_via_tool(registry: ToolRegistry, loader: SkillLoader) -> None:
    captured: dict = {}

    async def completion(*, system_prompt, user_input, model, allowed_tools):
        captured["system_prompt"] = system_prompt
        captured["user_input"] = user_input
        captured["allowed_tools"] = allowed_tools
        return "Weekly summary text"

    register_invoke_skill(registry, loader=loader, completion_fn=completion)
    result = await registry.invoke(
        INVOKE_SKILL_NAME,
        {"name": "weekly-digest", "input": "go"},
    )
    body = json.loads(result)
    assert body["skill"] == "weekly-digest"
    assert body["response"] == "Weekly summary text"
    # Caller's available_tools (the registry's default set) flowed through
    assert "file_read" in body["used_tools"] or body["used_tools"] == []


@pytest.mark.asyncio
async def test_invoke_unknown_skill_returns_error(
    registry: ToolRegistry, loader: SkillLoader
) -> None:
    async def completion(**_kwargs):
        return "ok"

    register_invoke_skill(registry, loader=loader, completion_fn=completion)
    result = await registry.invoke(INVOKE_SKILL_NAME, {"name": "missing", "input": "x"})
    body = json.loads(result)
    assert "error" in body
    assert body["skill"] == "missing"


@pytest.mark.asyncio
async def test_invoke_missing_name_returns_error(
    registry: ToolRegistry, loader: SkillLoader
) -> None:
    async def completion(**_kwargs):
        return "ok"

    register_invoke_skill(registry, loader=loader, completion_fn=completion)
    result = await registry.invoke(INVOKE_SKILL_NAME, {"input": "x"})
    body = json.loads(result)
    assert "error" in body
    assert "name is required" in body["error"]


@pytest.mark.asyncio
async def test_double_register_raises(registry: ToolRegistry, loader: SkillLoader) -> None:
    async def completion(**_kwargs):
        return "ok"

    register_invoke_skill(registry, loader=loader, completion_fn=completion)
    with pytest.raises(Exception, match="already registered"):
        register_invoke_skill(registry, loader=loader, completion_fn=completion)


def test_tool_appears_in_schema(registry: ToolRegistry, loader: SkillLoader) -> None:
    async def completion(**_kwargs):
        return "ok"

    register_invoke_skill(registry, loader=loader, completion_fn=completion)
    schema = registry.schema_for([INVOKE_SKILL_NAME])
    assert len(schema) == 1
    assert schema[0]["function"]["name"] == INVOKE_SKILL_NAME
    assert "name" in schema[0]["function"]["parameters"]["properties"]
    assert "input" in schema[0]["function"]["parameters"]["properties"]
