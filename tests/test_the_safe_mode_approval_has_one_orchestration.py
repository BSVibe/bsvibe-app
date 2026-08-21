"""감사 C1 — Safe-Mode 승인 오케스트레이션이 세 벌이고, **지금도 갈라져 있다**.

이 표면의 중복은 이미 두 번 실제 결함을 냈고, 코드가 그 역사를 적어뒀다:

* **Lift E40** — MCP 승인이 dispatch 를 아예 안 했다. 큐 행만 뒤집고
  ``dispatched=False`` 를 돌려주며 "워커가 다음 틱에" 를 믿었는데, **워커는
  ``delivery_events`` 를 드레인하지 실 safe_mode 큐를 보지 않는다.** 런은
  ``review_ready`` 에 닿았고 PR 은 열리지 않았다 (run 1079bff5, 2026-06-17).
* **#771** — MCP 승인만 **compensation handle 을 버렸다.** 그 딜리버러블은
  영원히 retract 불가였는데 호출은 ``dispatched=True`` 를 돌려줬다.
  *"Nothing looked wrong until someone tried to undo it."*

## 이 PR 이 찾은 **세 번째·네 번째** 갈래

=====================  ==============================  ==============================
                       REST                            MCP
=====================  ==============================  ==============================
dispatch 실패           **감싸지 않음** → HTTP 500       ``try/except`` → ``dispatched=False``
응답의 ``dispatched``   **``True`` 하드코딩**            실제 결과
=====================  ==============================  ==============================

REST 는 **승인이 이미 커밋된 뒤** dispatch 가 터지면 500 을 낸다. 형님은 에러를 보고
재시도하지만 그 항목은 더 이상 pending 이 아니라 **404** 다 — 승인은 됐고, 배달은
안 됐고, 그 사실을 알려주는 신호는 없다.

``artifact_type`` 해석도 **세 곳이 다 다르다** (REST 헬퍼 · MCP 헬퍼 ·
콜백의 ``session.get`` + ``"direct_output"`` 폴백).

## 통합 방향

오케스트레이션을 ``workflow.application.safe_mode_approval`` 하나로 모으고,
**결과 객체**를 돌려준다. 각 표면은 그 결과를 자기 오류/응답으로 옮긴다 —
HTTP 404/409 · ``ToolError`` · 콜백의 조용한 반환은 프로토콜의 몫이다 (C10 규칙).
"""

from __future__ import annotations

import inspect
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"

_SURFACES = (
    "backend/api/v1/safemode/mutations.py",
    "backend/mcp/tools/safe_mode_tools.py",
    "backend/connectors/approval_callback.py",
)


def test_only_the_shared_orchestration_dispatches_after_an_approve() -> None:
    """``dispatch_delivery`` 호출이 승인 표면들에는 없어야 한다 — 공유본에만."""
    offenders = [
        f"{p.relative_to(_ROOT)}:{i}"
        for rel in _SURFACES
        for p in [_ROOT / rel]
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "await dispatch_delivery(" in line
    ]
    assert not offenders, f"승인 표면이 직접 dispatch 한다: {offenders}"


def test_only_the_shared_orchestration_persists_the_compensation_handle() -> None:
    """#771 이 난 자리 — 보상 핸들 저장이 표면마다 있으면 또 하나가 빠뜨린다."""
    offenders = [
        f"{p.relative_to(_ROOT)}:{i}"
        for rel in _SURFACES
        for p in [_ROOT / rel]
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "await persist_compensation_handles(" in line
    ]
    assert not offenders, f"승인 표면이 직접 보상 핸들을 저장한다: {offenders}"


def test_the_shared_orchestration_returns_a_structured_outcome() -> None:
    from backend.workflow.application.safe_mode_approval import ApproveOutcome

    fields = set(ApproveOutcome.__dataclass_fields__)
    assert {"found", "approved", "dispatched", "deliverable_id"} <= fields


def test_the_rest_surface_no_longer_hardcodes_dispatched_true() -> None:
    """**드리프트 수정** — 배달이 안 됐는데 됐다고 말하면 안 된다."""
    src = (_ROOT / "backend/api/v1/safemode/mutations.py").read_text(encoding="utf-8")
    assert 'status="approved", dispatched=True' not in src
    assert "dispatched=True" not in src, "REST 가 아직 dispatched 를 하드코딩한다"


def test_dispatch_failure_does_not_revert_an_approval() -> None:
    """특성화 — 세 표면이 공유하던 불변식. 승인은 되돌리지 않는다."""
    from backend.workflow.application import safe_mode_approval as shared

    src = inspect.getsource(shared)
    assert "except Exception" in src, "dispatch 가 best-effort 로 감싸져야 한다"
    assert "rollback" not in src, "dispatch 실패가 승인을 되돌리면 안 된다"


def test_each_surface_keeps_its_own_error_shape() -> None:
    """양성 대조군 (C10 규칙) — 규칙은 공유하되 오류 표면은 프로토콜의 몫이다."""
    rest = (_ROOT / "backend/api/v1/safemode/mutations.py").read_text(encoding="utf-8")
    mcp = (_ROOT / "backend/mcp/tools/safe_mode_tools.py").read_text(encoding="utf-8")
    assert "HTTPException" in rest and "ToolError" not in rest
    assert "ToolError" in mcp and "HTTPException" not in mcp


def test_the_queue_primitive_is_untouched() -> None:
    """양성 대조군 — ``SafeModeQueue.approve/deny`` 는 이미 공유 원시연산이다.

    이 PR 은 그 **주변**만 모은다. 큐 자체를 건드리면 거절 사유가 볼트로 흘러가는
    경로(``_record_rejection_knowledge``)까지 흔들린다."""
    from backend.workflow.application.safe_mode_queue import SafeModeQueue

    for name in ("approve", "deny", "list_pending", "_record_rejection_knowledge"):
        assert hasattr(SafeModeQueue, name), name
