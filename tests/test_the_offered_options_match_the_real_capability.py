"""감사 A12 · B9 · D9 — 고를 수 있는 선택지가 실제 능력과 어긋난 곳 셋.

세 건 모두 **"메뉴가 아무도 구현 안 한 선택지를 판다"** 의 변종이다.

## A12 — 값이 하나뿐인 능력 협상

``required_methods`` / ``supported_methods`` 로 어댑터 호환성을 검사하는데,
**선언된 값이 전부 ``frozenset({"chat"})``** 이다 (caller 10곳 · adapter 2곳 실측).
``resolver.py`` 의 ``missing = spec.required_methods - adapter.supported_methods``
는 구조적으로 **항상 빈 집합**이다.

형님이 이미 세운 원칙이다 — *"가능한 능력이란걸 따로 정의할 필요가 있어?"*
툴/메서드 표면이 곧 능력의 정의이고, 그 위의 enum 은 **두 번째 소스**다 (INV-7).

## B9 — 고를 수 있는데 구현이 없는 선택지

``OntologyAction = Literal["retract", "correct"]`` 이고 MCP 툴이 그 타입을 그대로
노출한다. 그런데 ``RetractionService.issue`` 는 ``"correct"`` 를 받으면
``CorrectionUnavailableError`` 로 **거절한다** — *"The in-place field-rewrite editor
was never built."*

⚠️ **감사는 이걸 "도달 불가한 죽은 arm" 이라 적었지만 틀렸다.** 도달한다 —
사용자가 고를 수 있고, 정직한 에러를 받는다. 문제는 **메뉴에 있다는 것 자체**다.
고를 수 없게 만드는 것이 맞다.

## D9 — SoT 를 두고 두 번째 목록

``OUTPUT_MODES`` 가 ``identity.domain.repositories.resource_binding_repository`` 에
SoT 로 있고 SQL 어댑터가 그것을 import 한다. 그런데 ``mcp/tools/bindings_tools.py``
는 ``_VALID_OUTPUT_MODES = ("safe", "direct")`` 를 따로 적었다.

(PWA 의 ``types.ts`` 에도 사본이 있지만 별개 배포물이라 이 PR 의 대상이 아니다.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"


def _sites(needle: str) -> list[str]:
    return [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _BACKEND.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if needle in line
    ]


# ── A12 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "needle",
    [
        # **코드 형태**만 센다 — 왜 지웠는지 설명하는 산문(백틱 안의 이름)은 남아 있어야
        # 한다. 그 설명이 없으면 다음 사람이 같은 축을 다시 만든다.
        "required_methods=",
        "required_methods:",
        "required_methods -",
        ".supported_methods",
        "supported_methods:",
        "supported_methods =",
    ],
)
def test_the_single_valued_capability_negotiation_is_gone(needle: str) -> None:
    assert not _sites(needle), f"{needle!r} 이 아직 있다: {_sites(needle)}"


def test_the_dispatch_resolver_still_resolves_for_a_caller() -> None:
    """양성 대조군 — 협상만 걷어내고 **해석 자체**는 그대로여야 한다."""
    from backend.dispatch.resolver import ModelAccountResolver

    assert hasattr(ModelAccountResolver, "resolve_for")


def test_every_caller_spec_still_declares_its_identity() -> None:
    """양성 대조군 — caller 레지스트리(INV-7 이 걸린 그 표면)는 살아 있어야 한다."""
    from backend.dispatch import caller_registry as reg

    callers = [v for k, v in vars(reg).items() if k.startswith("CALLER_") and isinstance(v, str)]
    assert len(callers) >= 8, f"caller 상수가 사라졌다: {callers}"


# ── B9 ────────────────────────────────────────────────────────────────────


def test_the_unimplemented_ontology_action_is_no_longer_offered() -> None:
    from backend.knowledge.domain.retraction import OntologyAction

    assert getattr(OntologyAction, "__args__", ()) == ("retract",)


def test_the_service_no_longer_needs_a_refusal_branch() -> None:
    """메뉴에서 빠졌으니 거절 분기도 필요 없다 — 스키마 경계가 먼저 막는다."""
    assert not _sites('if action == "correct"')


def test_retraction_still_works() -> None:
    """양성 대조군 — 철회는 형님이 실제로 쓰는 기능이다."""
    import importlib

    svc = importlib.import_module("backend.knowledge.application.retraction_service")
    assert hasattr(svc, "RetractionService")


# ── D9 ────────────────────────────────────────────────────────────────────


def test_the_output_modes_list_is_declared_once() -> None:
    sites = _sites('frozenset({"safe", "direct"})') + _sites('("safe", "direct")')
    assert len(sites) == 1, f"output_mode 목록이 여러 곳에 있다: {sites}"
    assert sites[0].startswith("backend/identity/"), f"SoT 가 아닌 곳: {sites[0]}"


def test_the_mcp_binding_tool_uses_the_shared_list() -> None:
    from backend.identity.domain.repositories.resource_binding_repository import OUTPUT_MODES
    from backend.mcp.tools import bindings_tools as mcp

    assert mcp._VALID_OUTPUT_MODES is OUTPUT_MODES  # noqa: SLF001


def test_the_output_mode_values_are_unchanged() -> None:
    """양성 대조군 — ``resource_bindings.output_mode`` 에 이미 쌓인 값이다."""
    from backend.identity.domain.repositories.resource_binding_repository import OUTPUT_MODES

    assert OUTPUT_MODES == frozenset({"safe", "direct"})
