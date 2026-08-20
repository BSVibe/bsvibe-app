"""v8 §7.2/§7.3 상태기계를 지운다 — 약속된 H2 는 오지 않았고 어휘는 드리프트했다.

감사 A3·A4 (2026-08-19). ``workflow/domain/{state,transitions,_domain}.py`` 는
"H1 이 모양만 세우고 H2 가 채운다"는 전제로 만들어진 확장점이다. **H2 는 오지
않았다** — ``transitions.py`` 가 핸들러의 거처로 지목한
``backend.workflow.application._handlers`` 는 **`.py` 가 0개인 빈 폴더**였고
git 트리에는 존재조차 하지 않는다.

그 사이 프로덕션은 다른 어휘로 이미 돌고 있다. ``_domain.py`` 는 자기가 SoT 라고
적었고 ``infrastructure/db.py`` 는 자기가 그 미러라고 주석에 적었는데, **값이 아예
다르다** (감사 A4):

=====================  ==========================================  ==========================================
enum                   ``domain/_domain.py`` (죽은 쪽)               ``infrastructure/db.py`` (프로덕션)
=====================  ==========================================  ==========================================
런 상태                  ``RequestStatus`` — needs_decision/abandoned  ``RunStatus`` — **이름부터 다르다**, failed/cancelled
``WorkStepStatus``      needs_decision · verifying · review_ready     VERIFIED · REJECTED
``ProofState``          verification_missing · human_review_required  UNTESTED · PROVED · REFUTED
=====================  ==========================================  ==========================================

소비자를 심볼이 아니라 **import 경로**로 셌다 — 이름이 겹쳐서 grep 이 69건을
부풀린다. 세 모듈을 실제로 import 하는 것은 서로와 **이 두 테스트 파일뿐**이었다
(``test_state_projection.py`` · ``test_transitions.py``). 프로덕션 소비자 **0**.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_DEAD_MODULES = (
    "backend.workflow.domain.state",
    "backend.workflow.domain.transitions",
    "backend.workflow.domain._domain",
)

_ROOT = Path(__file__).resolve().parents[2]
_TREES = (_ROOT / "backend", _ROOT / "tests")


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_unreached_state_machine_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_source_still_points_at_the_deleted_modules() -> None:
    """주석의 dangling 참조와 문자열 import 까지 잡는다.

    ``backend/`` 만 봐서는 부족하다 — 세 번째 알리바이 테스트가
    ``importlib.import_module("backend.workflow.domain.state")`` 라는 **문자열**로
    죽은 모듈을 붙들고 있었고, import 문을 세는 grep 은 그것을 못 봤다.
    ``db.py`` 는 값이 아예 다른데 ``_domain`` 의 미러라고 주석에 적어뒀었다.
    """
    needles = ("workflow.domain.state", "workflow.domain.transitions", "workflow.domain._domain")
    offenders = [
        f"{path.relative_to(_ROOT)}:{i}"
        for tree in _TREES
        for path in tree.rglob("*.py")
        if path != Path(__file__)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(needle in line for needle in needles)
    ]
    assert not offenders, f"삭제된 모듈을 아직 가리킨다: {offenders}"


def test_the_persistent_vocabulary_survives() -> None:
    """양성 대조군 — 프로덕션이 실제로 읽고 쓰는 enum 은 ``infrastructure`` 쪽이다."""
    db = importlib.import_module("backend.workflow.infrastructure.db")
    intake = importlib.import_module("backend.workflow.infrastructure.intake.db")
    for name in ("RunStatus", "RunAttemptPhase", "WorkStepStatus", "ProofState"):
        assert hasattr(db, name), f"프로덕션 enum 이 사라졌다: db.{name}"
    assert hasattr(intake, "RequestStatus"), "프로덕션 enum 이 사라졌다: intake.db.RequestStatus"


def test_the_surviving_run_status_still_covers_what_prod_holds() -> None:
    """양성 대조군 — prod ``execution_runs.status`` 에 실제로 있는 값들 (2026-08-20 실측)."""
    from backend.workflow.infrastructure.db import RunStatus

    observed = {"open", "running", "review_ready", "shipped", "cancelled", "failed"}
    assert observed <= {member.value for member in RunStatus}
