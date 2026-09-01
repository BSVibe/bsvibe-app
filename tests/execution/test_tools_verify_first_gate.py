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


def test_declare_verification_topic_follows_output_language(tmp_path: Path) -> None:
    """KO-workspace regression: ``topic`` is user-facing prose (the note title),
    not an identifier, so its schema description must tell the model to write it
    in the SAME language as the rest of its output (the workspace language) — else
    the short label drifts to English while the note body localizes correctly."""
    registry = _registry(tmp_path)
    schema = registry.schema_for(["declare_verification"])[0]["function"]["parameters"]
    topic_desc = schema["properties"]["knowledge"]["properties"]["topic"]["description"].lower()
    assert "same language" in topic_desc


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


# ── 늦은 회고 선언 — 브리핑이 에이전트에게 시키는 바로 그 경로 ──────────────────
#
# 회고(``declared_knowledge``)에는 자기 툴이 없다. ``record_knowledge`` 는 코드에
# **주석으로만** 존재했고(``_drive_loop.py:489``), 유일한 채널은 이 툴의 OPTIONAL
# ``knowledge`` 인자다. 그런데 B7 게이트는 이 툴을 **쓰기 전에** 부르게 한다 — 그
# 시점에 에이전트는 아직 아무것도 배우지 않았다.
#
# 그래서 ``_SYSTEM_PROMPT`` 는 끝에서 한 번 더 부르라고 시킨다. 아래 둘은 그 지시가
# 참이 되게 하는 조건이며, **문구가 아니라 동작**을 고정한다.


async def test_knowledge_declared_after_the_work_is_latched(tmp_path: Path) -> None:
    """게이트를 만족시킨 첫 선언에는 회고가 없고, 작업을 마친 뒤의 재선언이 그것을 싣는다.

    이것이 prod 에서 회고가 생길 수 있는 유일한 모양이다. 재선언이 거부되거나 회고를
    못 실으면 브리핑의 지시는 에이전트를 막다른 길로 보낸다.
    """
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

    registry = _registry(tmp_path)
    await _declare(registry)  # 1st — 게이트를 연다. 배운 것은 아직 없다.
    assert registry.declared_knowledge is None
    await registry.invoke("file_write", {"path": "out.txt", "content": "work"})

    await registry.invoke(  # 2nd — 일을 마친 뒤. 같은 checks + 회고.
        "declare_verification",
        {
            "checks": [{"kind": "command", "command": "test -f out.txt"}],
            "knowledge": {"topic": "늦은 선언", "insight": "배운 뒤에야 회고할 수 있다."},
        },
    )
    assert registry.declared_knowledge == RememberableKnowledge(
        topic="늦은 선언", insight="배운 뒤에야 회고할 수 있다."
    )


async def test_redeclaring_replaces_the_contract_so_the_briefing_must_say_so(
    tmp_path: Path,
) -> None:
    """재선언은 계약을 **덮어쓴다** — 합치지 않는다.

    실측 2026-09-01. 이 사실 때문에 브리핑은 "checks 를 그대로 다시 내라"고 말해야
    한다. 처음 쓴 초안은 *"re-declaring keeps the contract you already made"* 였고,
    그대로 나갔다면 회고를 남기려던 에이전트가 stub check 로 재선언해 자기 진짜 계약을
    조용히 파괴했을 것이다.

    ⚠️ 이 테스트는 동작을 고정한다. 나중에 재선언이 **병합**으로 바뀌면 여기가 빨개지고,
    그때 브리핑 문구도 같이 고쳐야 한다는 뜻이다 — 산문과 동작이 갈라지지 못하게.
    """
    registry = _registry(tmp_path)
    await _declare(registry)
    assert registry.declared_contract is not None
    first = [c["command"] for c in registry.declared_contract["checks"]]
    assert first == ["test -f out.txt"]

    await registry.invoke(
        "declare_verification",
        {"checks": [{"kind": "command", "command": "true"}]},
    )
    assert registry.declared_contract is not None
    second = [c["command"] for c in registry.declared_contract["checks"]]
    assert second == ["true"], "재선언이 병합된다면 브리핑의 경고가 거짓이 된다"


def test_both_declare_surfaces_warn_that_redeclaring_replaces() -> None:
    """네이티브 레지스트리와 MCP 트랜스포트가 **같은** 경고를 싣는다.

    둘 다 *"You may call this again to refine the contract"* 라고 적혀 있었다.
    "refine" 은 덧붙이기로 읽히는데 실제 동작은 **덮어쓰기**다 — 브리핑이 재선언을
    시키기 시작한 이상, 에이전트가 재선언 직전에 읽는 자리에도 사실이 있어야 한다.

    ⚠️ 미러된 표면은 덜 쓰이는 쪽으로 갈라진다. 그래서 **한쪽만** 고치는 것을 여기서 막는다.
    """
    from backend.mcp.tools.work_tools import WORK_TOOL_FORWARDING_SPECS

    registry = ToolRegistry(workspace_dir=Path("."))
    # ⚠️ ``["parameters"]`` 만 보면 안 된다 — 네이티브 쪽은 그 문장을 **툴 description**
    # 에, MCP 쪽은 필드 description 에 싣는다. 명제는 "에이전트가 보는 스키마에 있다"이므로
    # function 전체를 본다.
    native_text = str(registry.schema_for(["declare_verification"])[0]["function"])

    mcp_schema = next(
        s["input_schema"]
        for s in WORK_TOOL_FORWARDING_SPECS
        if s["inner"] == "declare_verification"
    )
    mcp_text = str(mcp_schema.model_json_schema())

    for label, text in (("native", native_text), ("mcp", mcp_text)):
        assert "REPLACES the contract" in text, f"{label} surface lost the replace warning"
        assert "knowledge" in text, f"{label} surface no longer points at the knowledge block"
