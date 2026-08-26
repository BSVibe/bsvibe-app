"""선언이 0개일 때 무엇을 하는가 — 형님 판정(2026-08-20)을 코드로.

    "검증은 통과/실패 둘 뿐이야 오직. 이 때 실패와 명시적 없음은 달라.
     검증 할게 있었는데 실패한건 실패한거고.
     정말 검증할게 없어서 아무것도 안한거는 통과야."

∴ 이 축에 **제3상태(파킹)는 없다.** 그리고 형님이 그은 선은 "게이트가 있었나"가
아니라 **"증명할 것이 있었나"** 다. 오늘 그 자리는 ``assemble_contract`` 이 ``None``
을 내면 ``human_review_required`` / ``no_verification_declared`` Decision 을 세워
형님을 부른다 — 정확히 없어야 할 제3상태다.

판별 신호는 새로 만들 것이 아니라 이미 있다. ``inplace_gate.changed_paths`` 가 자기
docstring 에 용도를 *"did this run change anything at all"* 이라고 적어뒀다.

**왜 ``written_paths`` 로는 안 되는가 (이 테스트의 존재 이유).**
B7 verify-first 게이트는 ``file_write``/``file_edit`` 만 막고 ``shell_exec`` 은 막지
않는다. prod 실측 ``fae09a47``: ``shell_exec`` **62회**, 활동 로그의 ``writes`` 는
**전부 빈 배열**, 그런데 커밋은 **+108/−2**. 서버가 본 쓰기로 판단하면 그 런은
"아무것도 안 바꿨다"로 읽혀 검사 0개로 조용히 통과한다. git 에게 물어야 잡힌다.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.application.undeclared_verification import (
    settle_undeclared_verification,
)
from backend.workflow.infrastructure.db import VerificationOutcome

pytestmark = pytest.mark.asyncio


class _Exec:
    def __init__(self, stdout: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.timed_out = False


class _Box:
    """A tree that answers git. ``porcelain`` = uncommitted, ``diff`` = since baseline."""

    def __init__(self, porcelain: str = "", diff: str = "") -> None:
        self._porcelain = porcelain
        self._diff = diff
        self.queries: list[str] = []

    async def exec(self, command: str, **_kw: Any) -> _Exec:
        self.queries.append(command)
        if command.startswith("git status"):
            return _Exec(self._porcelain)
        return _Exec(self._diff)


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


class _Step:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


class _Verifier:
    """Stands in for VerificationService — records the question, answers as told."""

    def __init__(self, real_worktree: bool) -> None:
        self._answer = real_worktree
        self.asked: list[Any] = []

    def _is_real_worktree(self, run: Any) -> bool:
        self.asked.append(run)
        return self._answer


class _Run:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {}


class _Orch:
    """The exact surface the branch uses on the orchestrator — nothing more."""

    def __init__(self, real_worktree: bool = True) -> None:
        self._session = _Session()
        self.verifier = _Verifier(real_worktree)
        self.decisions: list[dict[str, Any]] = []
        self.audits: list[tuple[str, dict[str, Any]]] = []
        self.finished: dict[str, Any] | None = None

    async def _create_decision(self, _run, _step, *, kind, payload, rationale):  # noqa: ANN001, ANN002, ANN003
        self.decisions.append({"kind": kind, "payload": payload, "rationale": rationale})
        raise AssertionError("형님 판정: 이 축에 제3상태는 없다 — 선언 0 이 형님을 부르면 안 된다")

    async def _audit(self, _run, _attempt, event_cls, data):  # noqa: ANN001
        self.audits.append((event_cls.__name__, data))

    async def _finish_verified(self, run, work_step, attempt, written, text, verdict, knowledge):  # noqa: ANN001, ARG002, PLR0913
        self.finished = {"verdict": verdict, "written": list(written), "text": text}
        return "VERIFIED-TERMINAL"

    def _verifier(self) -> _Verifier:
        return self.verifier


async def _settle(orch: _Orch, box: _Box, messages: list[dict[str, Any]], **kw: Any) -> Any:
    return await settle_undeclared_verification(
        orch,
        run=kw.get("run") or _Run(),
        work_step=_Step(),
        attempt=object(),
        box=box,
        baseline=kw.get("baseline", "cafe1234"),
        written_paths=kw.get("written_paths", []),
        final_text=kw.get("final_text", "done"),
        messages=messages,
        knowledge=None,
    )


# ---------------------------------------------------------------------------
# 통과 — "정말 검증할게 없어서 아무것도 안한거는 통과야"
# ---------------------------------------------------------------------------


async def test_a_run_that_changed_nothing_passes() -> None:
    """증명할 것이 없었다 → 통과. 형님을 부르지 않는다."""
    orch, box, messages = _Orch(), _Box(porcelain="", diff=""), []

    result = await _settle(orch, box, messages)

    assert result == "VERIFIED-TERMINAL", "선언 0 + 변경 0 은 통과여야 한다"
    assert orch.decisions == [], "제3상태(형님 호출)는 이 축에 없다"
    assert messages == [], "통과인데 에이전트를 다시 돌릴 이유가 없다"


async def test_the_pass_persists_an_inspectable_record() -> None:
    """통과했다는 주장은 검사 가능해야 한다 — 단언이 아니라 레코드로.

    ``_finish_verified`` 는 ``verdict.id`` 를 ``LoopResult`` 에 싣고
    ``verdict.result`` 로 딜리버러블 요약을 만든다. 빈 것을 넘기면 그 두 곳이 조용히
    빈다."""
    orch, box, messages = _Orch(), _Box(), []

    await _settle(orch, box, messages)

    assert orch.finished is not None
    verdict = orch.finished["verdict"]
    assert verdict.outcome is VerificationOutcome.PASSED
    assert verdict in orch._session.added, "레코드가 세션에 들어가야 조회된다"
    assert verdict.result.get("undeclared_no_change") is True, (
        "왜 통과했는지가 레코드에 남아야 한다 — 나중에 이 통과를 세려면 필요하다"
    )


async def test_the_pass_still_tells_the_founder_the_evidence_is_weak() -> None:
    """#742 재발 방지 — 파킹은 없앴지만 형님 폰에 닿는 문장까지 없애면 안 된다.

    ``_weak_evidence_sentence`` 는 ``gate_applicable`` 이 falsy 면 침묵한다."""
    from backend.workflow.application._verified_summary import _weak_evidence_sentence

    orch, box = _Orch(real_worktree=True), _Box()
    await _settle(orch, box, [])

    result = orch.finished["verdict"].result  # type: ignore[index]
    assert "증거 약함" in _weak_evidence_sentence(result, ko=True), (
        "검사 0개로 통과했다는 사실은 형님에게 반드시 말해져야 한다"
    )


async def test_gate_applicable_is_the_same_question_verify_asks() -> None:
    """값을 지어내지 말고 ``verify`` 와 같은 출처에 물어야 한다.

    두 답을 다 통과시키는 테스트여야 의미가 있다 — 픽스처가 기대값을 건네주고
    그걸 되돌려받는 테스트는 어떤 구현이든 영원히 통과시킨다."""
    for real_worktree in (True, False):
        orch, run = _Orch(real_worktree=real_worktree), _Run()
        await _settle(orch, _Box(), [], run=run)

        result = orch.finished["verdict"].result  # type: ignore[index]
        assert result["gate_applicable"] is real_worktree
        assert orch.verifier.asked == [run], "verify 가 쓰는 그 판별식에 물어야 한다"


async def test_a_run_with_no_product_has_no_gate_concept() -> None:
    """``run.product_id is None`` 인 Direct 스크래치 답변은 게이트가 없다."""
    orch, run = _Orch(), _Run()
    run.product_id = None

    await _settle(orch, _Box(), [], run=run)

    assert orch.finished["verdict"].result["gate_applicable"] is False  # type: ignore[index]


# ---------------------------------------------------------------------------
# 실패 — 바꿔놓고 증명 방법을 말하지 않았다
# ---------------------------------------------------------------------------


async def test_a_run_that_changed_files_without_declaring_is_sent_back() -> None:
    """실패는 형님 호출이 아니라 **에이전트에게 되돌림**이다 — 라운드 캡이 종착점."""
    orch, box, messages = _Orch(), _Box(porcelain=" M backend/a.py"), []

    result = await _settle(orch, box, messages)

    assert result is None, "None = 루프 계속 — 에이전트가 선언할 기회를 갖는다"
    assert orch.decisions == [], "제3상태(형님 호출)는 이 축에 없다"
    assert len(messages) == 1, "에이전트가 무엇을 해야 하는지 정확히 한 번 듣는다"
    assert messages[0]["role"] == "user"
    assert "declare_verification" in messages[0]["content"]


async def test_the_agent_is_told_which_paths_it_must_prove() -> None:
    """무엇을 증명해야 하는지 모르면 같은 실패를 반복한다 (#742 blind-prefix 교훈)."""
    orch, box, messages = _Orch(), _Box(porcelain=" M backend/a.py\n?? backend/b.py"), []

    await _settle(orch, box, messages)

    content = messages[0]["content"]
    assert "backend/a.py" in content and "backend/b.py" in content


async def test_a_shell_exec_only_run_is_caught() -> None:
    """prod ``fae09a47`` 의 정확한 모양 — 서버가 본 쓰기는 0, git 은 108줄을 봤다.

    ``written_paths`` 로 판단했다면 이 런은 '아무것도 안 바꿨다'로 통과한다."""
    orch, box, messages = _Orch(), _Box(porcelain="", diff="backend/x.py\nbackend/y.py"), []

    result = await _settle(orch, box, messages, written_paths=[])

    assert result is None, "shell_exec 으로만 일한 런이 검사 0개로 통과하면 안 된다"
    assert "backend/x.py" in messages[0]["content"]


async def test_an_unanswerable_tree_is_not_read_as_no_change() -> None:
    """git 이 답을 못 하면 '변경 없음'이 아니다 — 배선 결함이 통과를 입는 그 모양.

    baseline 이 없고 porcelain 도 비면 ``changed_paths`` 는 빈 목록을 낸다. 그것이
    '깨끗한 트리'인지 '못 물어본 트리'인지 구분되는 유일한 신호가 ``written_paths``
    다: 서버가 쓰기를 봤는데 git 이 아무것도 못 봤다면 물음이 실패한 것이다."""
    orch, box, messages = _Orch(), _Box(porcelain="", diff=""), []

    result = await _settle(orch, box, messages, written_paths=["backend/z.py"], baseline=None)

    assert result is None, "서버가 쓰기를 본 런은 git 침묵을 이유로 통과할 수 없다"


# ---------------------------------------------------------------------------
# 구조 — 지워진 제3상태가 되살아나지 않게
# ---------------------------------------------------------------------------


async def test_the_loop_no_longer_parks_an_undeclared_run_on_the_founder() -> None:
    """문자열 가드 — 이 Decision 을 다시 세우는 것은 형님 판정의 되돌림이다."""
    import inspect

    from backend.workflow.application import _drive_loop

    source = inspect.getsource(_drive_loop)
    assert "no_verification_declared" not in source, (
        "선언 0 은 통과이거나 실패다 — 형님을 부르는 제3상태가 아니다"
    )


async def test_the_loop_actually_routes_the_undeclared_case_here() -> None:
    """위 두 테스트는 이 함수를 **직접** 부른다 — 루프가 정말 부르는지는 별개 사실이다.

    유닛 통과 + 프로덕션 호출자 0인 죽은 코드가 이 레포의 반복 결함이라
    (`feedback_queue_apply_step_never_wired`) 호출을 AST 로 못박는다."""
    import ast
    import inspect

    from backend.workflow.application import _drive_loop

    tree = ast.parse(inspect.getsource(_drive_loop))
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "settle_undeclared_verification" in called, (
        "drive_loop 이 이 판정을 부르지 않으면 이 모듈은 죽은 코드다"
    )
