"""B7 — verify-first gate at the ToolRegistry level.

The mutating file tools (``file_write`` / ``file_edit``) must REFUSE until a
verification contract has been declared via ``declare_verification`` at least
once in the run. Read-only tools (``file_read`` / ``file_list``) are never
gated. Once a contract is declared, the same write succeeds for the rest of
the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.workflow.infrastructure.tools import ToolError, ToolRegistry

_DECLARE_HINT = "declare_verification"


def _registry(tmp_path: Path) -> ToolRegistry:
    return ToolRegistry(workspace_dir=tmp_path)


async def _declare(registry: ToolRegistry) -> None:
    await registry.invoke(
        "declare_verification",
        {"checks": [{"kind": "command", "command": "test -f out.txt"}]},
    )


# -- v2: agent-declared knowledge captured off the contract -----------------


async def test_declare_verification_captures_declared_knowledge(tmp_path: Path) -> None:
    """v2 — when the agent includes a ``knowledge`` block in its verification
    contract (the retrospective-style declaration), the registry latches it as a
    RememberableKnowledge so the settle path can write it. The block is dropped
    from the verification contract itself (it isn't a check)."""
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

    registry = _registry(tmp_path)
    assert registry.declared_knowledge is None
    await registry.invoke(
        "declare_verification",
        {
            "checks": [{"kind": "command", "command": "pytest"}],
            "knowledge": {
                "topic": "Idempotent webhooks",
                "insight": "Dedupe webhook deliveries by event id — providers retry.",
            },
        },
    )
    assert registry.declared_knowledge == RememberableKnowledge(
        topic="Idempotent webhooks",
        insight="Dedupe webhook deliveries by event id — providers retry.",
    )
    # The contract itself carries only the checks (knowledge is not a check).
    assert "knowledge" not in (registry.declared_contract or {})


async def test_declare_verification_without_knowledge_leaves_none(tmp_path: Path) -> None:
    """Routine work: no knowledge block declared → nothing latched (no note)."""
    registry = _registry(tmp_path)
    await _declare(registry)
    assert registry.declared_knowledge is None


def test_declare_verification_schema_exposes_knowledge(tmp_path: Path) -> None:
    """v2 — the native LLM sees an OPTIONAL ``knowledge`` param on
    declare_verification (so it can declare a learning like the executor does in
    its contract). Not required — routine work omits it."""
    registry = _registry(tmp_path)
    schema = registry.schema_for(["declare_verification"])[0]["function"]["parameters"]
    props = schema["properties"]
    assert "knowledge" in props
    assert set(props["knowledge"]["properties"]) == {"topic", "insight"}
    assert "knowledge" not in schema["required"]


# -- the core delta: write/edit refused before declare ----------------------


async def test_file_write_refused_before_declare(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    with pytest.raises(ToolError) as excinfo:
        await registry.invoke("file_write", {"path": "out.txt", "content": "hi"})
    # Actionable refusal naming the unlock tool.
    assert _DECLARE_HINT in str(excinfo.value)
    # No file was written.
    assert not (tmp_path / "out.txt").exists()


async def test_file_edit_refused_before_declare(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    # Seed an existing file directly on disk (not via the gated tool).
    target = tmp_path / "src.txt"
    target.write_text("old content", encoding="utf-8")
    with pytest.raises(ToolError) as excinfo:
        await registry.invoke(
            "file_edit",
            {"path": "src.txt", "old_string": "old", "new_string": "new"},
        )
    assert _DECLARE_HINT in str(excinfo.value)
    # The file was NOT modified — gate fires before any read/write.
    assert target.read_text(encoding="utf-8") == "old content"


# -- declaring unlocks writes for the rest of the run -----------------------


async def test_file_write_succeeds_after_declare(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    await _declare(registry)
    result = await registry.invoke("file_write", {"path": "out.txt", "content": "42\n"})
    assert "wrote" in result
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "42\n"


async def test_file_edit_succeeds_after_declare(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    target = tmp_path / "src.txt"
    target.write_text("old content", encoding="utf-8")
    await _declare(registry)
    # file_edit still requires a prior file_read (grounding) — declare alone
    # unlocks the gate; the existing grounding rule is unchanged.
    await registry.invoke("file_read", {"path": "src.txt"})
    result = await registry.invoke(
        "file_edit",
        {"path": "src.txt", "old_string": "old", "new_string": "new"},
    )
    assert "edited" in result
    assert target.read_text(encoding="utf-8") == "new content"


async def test_declare_unlocks_writes_for_rest_of_run(tmp_path: Path) -> None:
    """A single declare unlocks every subsequent write — the gate is
    per-registry latch state, not per-call."""
    registry = _registry(tmp_path)
    await _declare(registry)
    await registry.invoke("file_write", {"path": "a.txt", "content": "1"})
    await registry.invoke("file_write", {"path": "b.txt", "content": "2"})
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "1"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "2"


# -- read-only tools are NOT gated ------------------------------------------


async def test_file_read_not_gated_before_declare(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    (tmp_path / "src.txt").write_text("readable", encoding="utf-8")
    result = await registry.invoke("file_read", {"path": "src.txt"})
    assert result == "readable"


async def test_file_list_not_gated_before_declare(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    result = await registry.invoke("file_list", {"path": "."})
    assert "a.txt" in result


def test_declare_verification_directive_steers_uv_run_and_format(tmp_path: Path) -> None:
    """The declare_verification directive must steer command checks through the
    project runner (`uv run …`) and remind the agent to format — dogfood
    dd2bd3a3: codex declared bare `python -m pytest` (→ "No module named
    pytest" in the uv sandbox) + never formatted, so verify looped to
    exhaustion."""
    reg = _registry(tmp_path)
    desc = reg.schema_for(["declare_verification"])[0]["function"]["description"]
    assert "uv run pytest" in desc
    assert "uv run ruff" in desc
    assert "ruff format" in desc
    # the anti-pattern is called out explicitly
    assert "No module named pytest" in desc
