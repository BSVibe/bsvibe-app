"""추월당한 두 표면을 지운다 — ``VerifierWorker`` 와 벌크 Safe Mode 만료.

`~/Docs/BSVibe_Reality_Audit_2026-07-14.md` 의 **5-4** 와 **5-12**. 그 문서는
STALE 배너를 달고 있어 **재측정 후에** 착수했다 — 같은 표의 5-11
(``SafeModeBoundary.gate``)은 이미 해소돼 있었고, MCP parity 5갭도 대부분
메워져 있었다. 이 둘만 여전히 열려 있었다.

## 재측정 (2026-08-29)

**5-4 ``VerifierWorker``** — 프로덕션 import **0**. 런타임 워커 목록
(``worker_runtime.py``)에 없다. 살아 있는 검증 경로는
``backend.workflow.application.verification_service`` 이고 그쪽은 널리 참조된다.
등록하는 것이 답이 아닌 이유: 인라인 처리와 **claim 경합**이 생긴다.

**5-12 벌크 만료** — ``SafeModeQueue.expire`` / ``repo.mark_expired_bulk``
프로덕션 호출자 **0**. 살아 있는 sweep 은 ``SafeModeExpirySweepRunner`` 이고,
그 파일이 스스로 적어 뒀다 — *"Goes through ``SafeModeQueue.mark_expired``
**per row** (NOT a bulk ...)"*.

**둘 다 테스트만이 살려 두고 있었다.** 전제가 거짓이 된 뒤에도 동작을 고정하는
알리바이 — 이 저장소에서 여섯 번 나온 그 모양이다.

## 덤으로 정리한 썩은 참조

``safe_mode_queue.py`` 의 docstring 이 ``expire_all_due`` 를 가리키는데
**그런 메서드는 존재하지 않는다**(트리 전체에서 정의 0). 죽은 표면을 지우면서
그 표면을 설명하던 산문도 같이 지운다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: 이름을 조립한다 — 일괄 rename/삭제 스크립트가 이 가드 자신을 오염시키지
#: 않도록 (#809 에서 needle 이 아니라 조건 줄이 통째로 지워진 적이 있다).
_WORKER = "Verifier" + "Worker"
_BULK = "mark_expired" + "_bulk"

_SCANNED = ("backend", "plugin", "bsvibe_sdk", "tests")


def _identifiers(tree: ast.AST):
    """실제 식별자만 낸다 — docstring·주석·산문은 애초에 후보가 아니다.

    #845 가 얻은 교훈: grep 을 넓게 잡으면 *왜 이걸 지웠는지 설명하는 산문*을
    자기가 물어서 가드가 영원히 빨갛다. AST 를 걸으면 그 문제가 사라진다.
    """
    for node in ast.walk(tree):
        match node:
            case ast.arg(arg=name) | ast.keyword(arg=str() as name):
                yield name
            case ast.Attribute(attr=name) | ast.Name(id=name):
                yield name
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                yield name
            case ast.ClassDef(name=name):
                yield name


def _scan(needles: set[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for root_name in _SCANNED:
        root = _ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path == Path(__file__) or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = {n for n in _identifiers(tree) if n in needles}
            if hits:
                found[str(path.relative_to(_ROOT))] = hits
    return found


def test_the_unregistered_verifier_worker_is_gone() -> None:
    """모듈 파일과 그 심볼이 트리 어디에도 남지 않아야 한다."""
    assert not (_ROOT / "backend/workflow/infrastructure/workers/verifier_worker.py").exists()
    offenders = _scan({_WORKER, "VerifierAdapter", "VerifierConfig"})
    assert not offenders, f"{_WORKER} 참조가 남았다:\n" + "\n".join(
        f"  {f}: {sorted(s)}" for f, s in sorted(offenders.items())
    )


def test_the_superseded_bulk_expiry_is_gone() -> None:
    """벌크 변형이 사라져야 한다 — per-row sweep 이 진짜 경로다."""
    offenders = _scan({_BULK})
    assert not offenders, f"{_BULK} 참조가 남았다:\n" + "\n".join(
        f"  {f}: {sorted(s)}" for f, s in sorted(offenders.items())
    )


#: 이 PR 이 지운 메서드 이름들. Sphinx 상호참조가 이들을 가리키면 그 링크는
#: 아무 데도 닿지 않는다.
_DEAD_METHODS = ("expire_all" + "_due", "SafeModeQueue.expire", "mark_expired" + "_bulk")


def test_no_cross_reference_points_at_a_method_that_does_not_exist() -> None:
    """죽은 링크를 잡는다 — **언급**이 아니라 **가리킴**이 결함이다.

    첫 판은 ``expire_all_due`` 한 이름만 텍스트로 셌다. 그러다 이 PR 자신이
    같은 썩음을 **새로 만들었다**: ``SafeModeQueue.expire`` 를 지웠는데
    ``safe_mode_expiry.py`` 와 ``safe_mode_queue.py`` 의 docstring 이 여전히
    그것을 ``:meth:`` 로 가리키고 있었고, 그중 하나는 *"Per-workspace callers
    should keep using ``expire``"* 라며 **없는 메서드를 쓰라고 지시**했다.
    유령 이름 하나만 박아둔 가드는 **자기 PR 이 만든 유령을 못 잡는다.**

    그래서 규칙을 바꿨다: 지운 이름을 **왜 지웠는지 서술하는 산문은 정당**하고
    (이 파일이 그렇다), 정당하지 않은 것은 ``:meth:`` **상호참조**다 — 그건
    독자를 없는 곳으로 보내는 포인터다.
    """
    survivors: list[str] = []
    for name in _SCANNED:
        root = _ROOT / name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path == Path(__file__):
                continue
            text = path.read_text(encoding="utf-8")
            survivors += [
                f"{path.relative_to(_ROOT)} -> {dead}"
                for dead in _DEAD_METHODS
                if f":meth:`{dead}`" in text or f":meth:`~{dead}`" in text
            ]
    assert not survivors, "아무 데도 닿지 않는 상호참조:\n  " + "\n  ".join(survivors)


def test_the_ghost_method_name_is_gone_from_prose_too() -> None:
    """``expire_all_due`` 는 **한 번도 존재한 적이 없다** — 언급 자체가 오해다.

    지워진 것을 서술하는 산문(``SafeModeQueue.expire``)과는 다르다: 이 이름은
    독자에게 *있었던 것*이라는 인상마저 주면 안 된다.
    """
    ghost = "expire_all" + "_due"
    survivors = [
        str(p.relative_to(_ROOT))
        for name in _SCANNED
        if (_ROOT / name).is_dir()
        for p in (_ROOT / name).rglob("*.py")
        if p != Path(__file__) and ghost in p.read_text(encoding="utf-8")
    ]
    assert not survivors, f"존재한 적 없는 {ghost} 를 언급하는 파일: {survivors}"


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_scanner_can_actually_find_a_surviving_worker() -> None:
    """**스캐너가 고장 나 있으면 위 부재 주장은 공짜로 참이 된다.**

    0을 세는 건 스캐너가 아무것도 못 찾을 때 가장 쉽다. 살아남아야 하는 워커로
    같은 스캐너를 먼저 돌려 실제로 식별자를 찾아냄을 증명한다.
    """
    found = _scan({"DeliveryWorker"})
    assert len(found) >= 2, f"스캐너가 살아 있는 워커를 못 찾는다: {sorted(found)}"


def test_the_live_verification_path_still_stands() -> None:
    """양성 대조군 — 검증 자체를 떼면 안 된다. 지우는 건 **미등록 중복**이다."""
    from backend.workflow.application.verification_service import VerificationService

    assert hasattr(VerificationService, "verify")


def test_the_live_expiry_sweep_still_goes_per_row() -> None:
    """양성 대조군 — 벌크를 지우면서 만료 자체를 잃으면 안 된다.

    살아 있는 경로가 per-row ``mark_expired`` 라는 것이 5-12 삭제의 **근거**
    자체다. 그러니 명제 그대로 박아둔다: 이게 깨지면 삭제의 전제가 바뀐 것이다.
    """
    from backend.workflow.application.safe_mode_expiry import SafeModeExpirySweepRunner
    from backend.workflow.application.safe_mode_queue import SafeModeQueue

    assert hasattr(SafeModeQueue, "mark_expired")
    source = Path(
        __import__("backend.workflow.application.safe_mode_expiry", fromlist=["x"]).__file__
    ).read_text(encoding="utf-8")
    assert "mark_expired(" in source
    assert SafeModeExpirySweepRunner is not None


def test_the_registered_workers_are_untouched() -> None:
    """양성 대조군 — 런타임 워커 목록이 이 삭제로 줄면 안 된다.

    ``VerifierWorker`` 는 **애초에 여기 없었다**. 그게 삭제의 근거다.
    """
    source = (_ROOT / "backend/workflow/application/runtime/worker_runtime.py").read_text(
        encoding="utf-8"
    )
    for name in ("IntakeWorker", "AgentWorker", "DeliveryWorker", "NotifyWorker", "SettleWorker"):
        assert f"{name}(" in source, f"{name} 등록이 사라졌다"
