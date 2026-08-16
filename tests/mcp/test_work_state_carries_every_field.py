"""워크툴 상태가 레지스트리의 모든 필드를 실어 나른다.

**prod 에서 잡힌 결함** (2026-08-16). A-2a 관측 모드를 배포하고 재보니
런의 ``declaration_patterns`` 가 **0** 이었다. 검색이 못 찾은 줄 알았는데
로그는 정반대를 말했다:

    {"found": 8, "signal_chars": 550, "event": "declaration_patterns_consulted"}

네 번 다 8건을 찾았다. **조회는 성공했고 저장이 실패했다.**

원인은 ``_merge_work_tool_state`` 가 필드를 **화이트리스트로 재구성**하는 것이다:

    return {
        "declared_contract": ...,
        "declared_knowledge": ...,
        "grounded_paths": ...,
        "written_paths": ...,
    }              # ← 여기 없는 필드는 조용히 사라진다

``export_state`` 에 필드를 더해도 이 머지를 통과하지 못하면 MCP 경로에서는
**없는 것과 같다.** 그리고 MCP 가 프로덕션 실행 경로다.

이 테스트는 그 결함을 특정 필드가 아니라 **구조로** 막는다 — export 가 내보내는
키는 머지를 **전부** 통과해야 한다. 다음에 필드를 하나 더 붙이는 사람이 같은 곳에서
같은 방식으로 조용히 잃지 않도록.

> 상태와 로그를 **둘 다** 남긴 것이 이 결함을 드러냈다. 하나만 봤으면
> "검색이 한국어를 못 읽는다"로 정반대 오진을 했을 것이다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.mcp.tools.work_registry import _merge_work_tool_state
from backend.workflow.application.tool_registry import assemble_run_tool_registry

pytestmark = pytest.mark.asyncio


async def test_the_merge_preserves_every_exported_key(tmp_path: Path) -> None:
    """export 가 내보내는 키는 하나도 빠짐없이 머지를 통과해야 한다.

    화이트리스트 머지는 새 필드를 **조용히** 버린다 — 예외도 로그도 없이.
    필드별로 테스트하면 다음 필드가 또 빠지므로 구조로 막는다.
    """
    registry = assemble_run_tool_registry(workspace_dir=tmp_path, sandbox=None)
    exported = registry.export_state()

    merged = _merge_work_tool_state(current={}, incoming=exported)

    missing = set(exported) - set(merged)
    assert not missing, f"머지가 버리는 키: {sorted(missing)}"


async def test_declaration_patterns_survive_the_merge() -> None:
    """A-2a 의 관측치가 런에 실제로 도달한다 (prod 에서 0 이던 그 값)."""
    merged = _merge_work_tool_state(
        current={},
        incoming={"declaration_patterns": ["Avoid (prior rejection) — 읽는 쪽까지 배선해라"]},
    )

    assert merged["declaration_patterns"] == ["Avoid (prior rejection) — 읽는 쪽까지 배선해라"]


async def test_a_stale_call_does_not_erase_the_patterns() -> None:
    """머지의 존재 이유는 병렬 툴 콜이 서로를 지우지 않게 하는 것이다.
    아무것도 모르는 콜이 이미 기록된 관측치를 지우면 안 된다."""
    merged = _merge_work_tool_state(
        current={"declaration_patterns": ["기록된 것"]},
        incoming={},
    )

    assert merged["declaration_patterns"] == ["기록된 것"]


async def test_a_fresh_consult_replaces_the_previous_one() -> None:
    """재선언하면 그 시점의 패턴이 맞다 — 오래된 것을 남기지 않는다
    (레지스트리의 재선언 동작과 같은 규칙)."""
    merged = _merge_work_tool_state(
        current={"declaration_patterns": ["옛것"]},
        incoming={"declaration_patterns": ["새것"]},
    )

    assert merged["declaration_patterns"] == ["새것"]
