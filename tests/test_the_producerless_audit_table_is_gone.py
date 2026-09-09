"""producer 없는 ``audit_events`` 를 지운다 — 한 번도 행을 담은 적 없는 자리.

**이름 충돌이 이 발견을 두 번 가렸다.** ``audit_events`` 는 **세 곳**에 있다:

=============================================  ===============  ==========  ========
심볼 / 모듈                                      무엇              prod 행수    판정
=============================================  ===============  ==========  ========
``plugin.audit.models.AuditEvent``             **테이블**                 0   삭제
``backend.workflow.application.audit_events``  이벤트 dataclass  (테이블 아님)  현역
``backend.api.v1.chat_audit_events``           챗 이벤트 모듈     (테이블 아님)  현역
=============================================  ===============  ==========  ========

이름으로 grep 하면 41건이 나오고 대부분이 뒤의 둘이다. 그래서 테이블이 죽었다는
사실이 **두 번 실측되고도**(2026-08-16 · 2026-09-09) 자리는 계속 남아 있었다.
같은 함정을 이 저장소는 ``WorkerRow`` 로 이미 한 번 겪었다 —
``tests/test_the_dead_worker_tables_are_gone.py``.

실측 (2026-09-09, 2026-08-16 과 일치):

=========================================================  =====
프로덕션 생성 지점 · select · delete                             0
prod ``audit_events`` 행                                         0
같은 통계 창의 INSERT (대조군 ``audit_outbox`` 는 687)             0
=========================================================  =====

살아 있는 감사 흔적은 ``audit_outbox``(prod 5,494행)다. EventBus 재배선(v8 §D5)
이후 producer 는 ``safe_emit`` → ``audit.emit`` → 아웃박스로 가고, 이 테이블은
BSupervisor 에서 lift 될 때 따라온 뒤 **아무도 겨누지 않았다.**

⚠️ #905 까지 이 테이블은 **GDPR Art. 30 보존 약속의 주어**였다 — 한 번도 바이트를
담은 적 없는 테이블에 "1년 보관"을 약속했다. 그 문장은 ``audit_outbox`` +
``audit_retention_days`` 로 정정됐고, 이 PR 은 자리를 지운다.

같은 근거로 이미 지운 것들: ``routing_logs`` · ``account_budget_policies`` ·
정규화/검색 미러 5개 · ``workers``/``worker_install_tokens``/``audit_relay_state``.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

#: 테이블 이름. 이 파일이 자기 스캔에 걸리지 않도록 조립한다 (저장소 관용구 —
#: ``tests/workflow/test_liftI0_execution_remnants.py`` 가 같은 방식을 쓴다).
_DEAD_TABLE = "audit_" + "events"

_DEAD_NAMES = (
    ("plugin.audit.models", "AuditEvent"),
    ("plugin.audit", "AuditEvent"),
)

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (_ROOT / "backend", _ROOT / "plugin", _ROOT / "tests")

#: 지운 뒤에도 이 리터럴을 정당하게 담는 파일. **패턴이 아니라 파일 집합을 핀으로
#: 박는다** — 어떤 철자로든 새 인스턴스가 생기면 목록에 없어서 실패한다.
#: 마이그레이션(만든 것 + 지우는 것)은 ``_TREES`` 스캔에서 경로로 제외된다.
_ALLOWED = {
    # Art.30 기록이 이 테이블을 **이름으로 부르지 않는지** 보는 음성 대조군.
    # 부재를 단언하는 파일이므로 리터럴이 남는 것이 옳다.
    "tests/api/test_v1_workspace_compliance.py",
}


@pytest.mark.parametrize(("module", "name"), _DEAD_NAMES)
def test_the_producerless_orm_is_gone(module: str, name: str) -> None:
    assert not hasattr(importlib.import_module(module), name), f"{module}.{name} 가 아직 있다"


def test_the_package_re_exports_none_of_it() -> None:
    """``__init__`` 이 계속 내보내면 삭제가 절반만 된 것이다."""
    pkg = importlib.import_module("plugin.audit")
    assert "AuditEvent" not in pkg.__all__, "plugin.audit.__all__ 이 아직 내보낸다"


def test_the_table_left_the_metadata() -> None:
    """ORM 심볼이 아니라 **메타데이터**를 본다 — 마이그레이션이 보는 것이 이쪽이다."""
    from plugin.audit.models import SupervisorBase

    assert _DEAD_TABLE not in SupervisorBase.metadata.tables


#: 지운 ORM 심볼. ``AuditEventBase`` / ``AuditEventSubscriber`` 는 **현역**이라
#: 접두사 매칭으로는 못 센다 — 뒤에 식별자 문자가 오지 않는 것만 잡는다.
_DEAD_SYMBOL = re.compile(r"\bAuditEvent(?![A-Za-z0-9_])")


def _scan(predicate) -> list[str]:
    return [
        f"{path.relative_to(_ROOT)}:{i}"
        for tree in _TREES
        for path in tree.rglob("*.py")
        if path != Path(__file__)
        and "migrations" not in path.parts
        and str(path.relative_to(_ROOT)) not in _ALLOWED
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if predicate(line)
    ]


def test_no_source_still_names_the_table() -> None:
    """철자 목록이 아니라 **파일 집합**을 핀으로 박는다."""
    needles = (f'"{_DEAD_TABLE}"', f"'{_DEAD_TABLE}'", f"ix_{_DEAD_TABLE}_")
    offenders = _scan(lambda line: any(needle in line for needle in needles))
    assert not offenders, f"지운 테이블을 아직 이름으로 부른다: {offenders}"


def test_no_source_still_names_the_orm_symbol() -> None:
    """**두 번째 축.** 테이블 리터럴만 물었을 때 이 축이 통째로 살아남았다.

    첫 판본은 ``"audit_events"`` 리터럴만 봤고, 심볼로 import 하던 네 파일
    (``plugin/audit/tests/test_models.py`` · ``tests/test_bundle1_imports.py`` ·
    ``tests/extensions/test_lift_r2a_audit_relocation.py`` ·
    ``tests/extensions/test_import_surface.py``)을 전부 통과시켰다. 스위트가
    수집 단계에서 잡아줬을 뿐이다 — **가드는 자기가 상상한 축만 증명한다.**
    """
    offenders = _scan(lambda line: _DEAD_SYMBOL.search(line) is not None)
    assert not offenders, f"지운 ORM 심볼을 아직 가리킨다: {offenders}"


def test_the_allowlist_is_not_stale() -> None:
    """핀이 썩는 것도 막는다 — 면제 파일이 실제로 그 리터럴을 담고 있어야 한다.

    담지 않게 되면 면제는 **아무 이유 없이 넓어진 구멍**이다.
    """
    for rel in _ALLOWED:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert _DEAD_TABLE in text, f"면제가 낡았다 — {rel} 는 더 이상 리터럴을 담지 않는다"


def test_the_live_outbox_survives() -> None:
    """양성 대조군 — 진짜 감사 흔적. prod 5,494행, 릴레이가 중앙 싱크로 보낸다."""
    from plugin.audit.models import AuditOutboxRecord, SupervisorBase

    assert AuditOutboxRecord.__tablename__ == "audit_outbox"
    assert "audit_outbox" in SupervisorBase.metadata.tables


def test_the_live_audit_event_modules_survive() -> None:
    """양성 대조군 — **이름이 같은 살아 있는 쪽 둘.** 이 삭제가 건드리면 안 된다."""
    workflow_events = importlib.import_module("backend.workflow.application.audit_events")
    for name in ("LoopTerminal", "DecisionResolved", "RoundBudgetDeclared"):
        assert hasattr(workflow_events, name), f"workflow audit_events 에서 {name} 이 사라졌다"

    pkg = importlib.import_module("plugin.audit")
    for name in ("AuditEventBase", "AuditEventSubscriber", "safe_emit", "OutboxStore"):
        assert hasattr(pkg, name), f"plugin.audit 에서 {name} 이 사라졌다"
