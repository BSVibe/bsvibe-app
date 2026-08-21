"""감사 C10 — Direct 제출의 제품 해석 규칙(L-P1)이 두 벌이고, 이미 갈라져 있었다.

MCP 쪽이 스스로 적어뒀다 — *"Mirror L-P1 product-resolution logic from the REST
messages endpoint."* 그런데 **미러가 아니었다**:

==================  ==========================  ==========================
                    REST                        MCP
==================  ==========================  ==========================
받는 입력             ``uuid.UUID | None``        ``str | None`` (**슬러그도**)
명시 id 조회          ``select ... where id``     ``session.get`` + 슬러그 조회
빈 워크스페이스         HTTP 400                    ``ToolError``
==================  ==========================  ==========================

같은 규칙을 두 번 적었고, 한쪽이 슬러그 지원을 얻는 동안 다른 쪽은 못 얻었다.

## 무엇을 공유하고 무엇을 남기나

**규칙은 공유하고 오류 표면은 남긴다.** "명시 id 가 이 워크스페이스 것이면 그것,
아니면 가장 먼저 만들어진 제품, 그것도 없으면 없음" 이 규칙이고 —
HTTP 400 이냐 ``ToolError`` 냐는 프로토콜의 몫이라 각자 유지한다. 공유 헬퍼는
``uuid.UUID | None`` 을 돌려주고, 부르는 쪽이 자기 오류를 던진다.

## 왜 ``backend.identity`` 인가

``ProductRow`` 가 거기 산다. 그리고 MCP 계약이 **명시적으로 허용**하는 컨텍스트다 —
*"MCP context depends only on **Identity** + Workflow + Knowledge + common"*.
계약 예외 순증 0.
"""

from __future__ import annotations

import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"


def test_the_default_product_query_is_written_once() -> None:
    """L-P1 의 "가장 먼저 만들어진 제품" 조회가 트리 전체에 **한 곳**이어야 한다."""
    sites = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _BACKEND.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "ProductRow.created_at.asc()" in line
    ]
    assert len(sites) == 1, f"기본 제품 조회가 여러 곳에 있다: {sites}"
    assert sites[0].startswith("backend/identity/"), f"소유 컨텍스트가 아니다: {sites[0]}"


def test_both_surfaces_call_the_shared_rule() -> None:
    """특성화 — 두 표면이 **같은 함수**를 부른다."""
    import inspect

    from backend.api.v1 import messages as rest
    from backend.mcp.tools import direct_tools as mcp

    for mod in (rest, mcp):
        assert "resolve_product_for_workspace" in inspect.getsource(mod), mod.__name__


def test_the_error_surfaces_stay_protocol_specific() -> None:
    """양성 대조군 — 규칙은 공유하되 **오류 표면은 각자**여야 한다.

    HTTP 400 을 MCP 가 던지거나 ``ToolError`` 를 REST 가 던지면 프로토콜이 깨진다."""
    import inspect

    from backend.api.v1 import messages as rest
    from backend.mcp.tools import direct_tools as mcp

    assert "ToolError" not in inspect.getsource(rest._resolve_product_id)  # noqa: SLF001
    assert "HTTPException" not in inspect.getsource(mcp._resolve_product_id)  # noqa: SLF001


def test_the_shared_rule_prefers_an_explicit_id_then_the_earliest_product() -> None:
    """특성화 — 규칙 자체를 문서화한다 (호출 시그니처가 세 갈래를 다 받는다)."""
    import inspect

    from backend.identity.product_resolution import resolve_product_for_workspace

    sig = inspect.signature(resolve_product_for_workspace)
    assert set(sig.parameters) >= {"session", "workspace_id", "slug_or_id"}
    # 반환은 "없으면 None" — 부르는 쪽이 자기 오류를 던진다.
    assert sig.return_annotation in (uuid.UUID | None, "uuid.UUID | None")


def test_the_mcp_superset_survives() -> None:
    """양성 대조군 — MCP 는 **슬러그로도** 제품을 지목할 수 있었다.

    통합하면서 그 능력을 잃으면 ``bsvibe_direct(product_slug_or_id="my-app")`` 이 깨진다."""
    import inspect

    from backend.identity.product_resolution import resolve_product_for_workspace

    src = inspect.getsource(resolve_product_for_workspace)
    assert "ProductRow.slug" in src, "슬러그 조회가 공유 규칙에서 사라졌다"
