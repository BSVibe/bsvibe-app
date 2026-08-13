"""머지워치가 포기했을 때 형님이 실제로 보는 것 — ``merge_watch_stalled``.

머지워치의 종료 4곳 중 ``conflict_unresolved_escalated`` 하나만 사람에게 말을
걸었다. 나머지 셋(``github_binding_unavailable`` 2곳 · ``ci_deadline_exceeded``)은
로그만 남기고 열린 PR 을 조용히 버렸다 — #742 와 같은 모양의 구멍이다.

이 파일은 **소비자가 무엇을 보는지**를 단언한다(생산자 쪽이 아니라):

* 브리핑의 needs-you 항목이 빈 질문이 아니라 **이유별로 다른** 로컬라이즈 문장을
  보여준다 (``_question_text``),
* 그 항목에 누를 수 있는 버튼이 달려 있다 (``_decision_actions``),
* 폰에 가는 푸시 본문이 영어 rationale 누출도, 일반 fallback 도 아닌 **이유별
  로컬라이즈 문장**이다 (``needs_you_reason_body``).
"""

from __future__ import annotations

from typing import Any

from backend.notifications.copy import needs_you_reason_body, notification_copy
from backend.workflow.application._checkpoint_shared import (
    _EXECUTOR_DECISION_ACTIONS,
    ACTION_ACKNOWLEDGE,
    ACTION_DISCARD,
    ACTION_RETRY,
    ACTION_SHIP,
    _decision_actions,
    _question_text,
)

#: 머지워치가 포기하는 두 가지 이유 — 로그/row.last_error 와 같은 어휘를 쓴다
#: (decision.payload["reason"] ≡ row.last_error 라서 디버깅이 한 줄로 이어진다).
_REASONS = ("github_binding_unavailable", "ci_deadline_exceeded")


class _Decision:
    def __init__(self, kind: str, payload: dict[str, Any] | None) -> None:
        self.decision = kind
        self.payload = payload


def _stalled(reason: str) -> _Decision:
    return _Decision("merge_watch_stalled", {"reason": reason, "repo": "acme/x", "pr_number": 23})


# --- 브리핑: 빈 질문이 아니라 이유별 문장 ------------------------------------


def test_each_reason_reads_differently_in_both_locales() -> None:
    """두 이유는 서로 다른 상황이다 — 같은 문장으로 뭉뚱그리면 형님이 무엇을
    해야 하는지 알 수 없다(커넥터를 고칠 일 vs CI 를 볼 일)."""
    lines = {
        (reason, lang): _question_text(_stalled(reason), lang)
        for reason in _REASONS
        for lang in ("en", "ko")
    }
    for text in lines.values():
        assert text.strip(), "needs-you 항목이 빈 질문으로 렌더된다"
    # 언어별로 다르고(로컬라이즈됨), 이유별로도 다르다(뭉뚱그리지 않음).
    for reason in _REASONS:
        assert lines[(reason, "en")] != lines[(reason, "ko")]
    assert lines[("github_binding_unavailable", "ko")] != lines[("ci_deadline_exceeded", "ko")]


def test_unknown_reason_still_reads_as_the_kind_line() -> None:
    """이유를 모르는(또는 이유 없는) stalled Decision 도 빈 질문이 되면 안 된다."""
    assert _question_text(_Decision("merge_watch_stalled", {}), "ko").strip()
    assert _question_text(_Decision("merge_watch_stalled", {"reason": "???"}), "en").strip()


def test_recorded_question_still_rides_through_verbatim() -> None:
    d = _Decision("merge_watch_stalled", {"reason": "ci_deadline_exceeded", "question": "X?"})
    assert _question_text(d, "ko") == "X?"


# --- 브리핑: 누를 수 있는 버튼 ----------------------------------------------


def test_stalled_offers_acknowledge_only() -> None:
    """런은 이미 끝났고(딜리버러블은 착지했다) 남은 것은 머지되지 않은 PR 이다.
    ``ship`` 할 것도(머지 안 됨) ``retry`` 로 다시 몰 런도 없다 — 형님이 GitHub
    쪽에서 처리하고 '확인했다'고 접는 것이 정직한 유일한 동작이다."""
    actions = _EXECUTOR_DECISION_ACTIONS["merge_watch_stalled"]
    keys = {a.key for a in actions}
    assert keys == {ACTION_ACKNOWLEDGE}
    assert ACTION_SHIP not in keys
    assert ACTION_RETRY not in keys
    assert ACTION_DISCARD not in keys
    for a in actions:
        assert a.label_en and a.label_ko
    assert _decision_actions(_stalled("ci_deadline_exceeded")) is not None


# --- 폰: 푸시 본문 -----------------------------------------------------------


def test_push_body_is_localized_per_reason_not_the_generic_fallback() -> None:
    """PR#610 의 재발 방지 — 영어 rationale 도, "작업이 멈췄고 결정을 기다리고
    있어요" 라는 일반 fallback 도 아닌, 이유별 문장이 폰에 가야 한다."""
    generic_ko = notification_copy("needs_you", "ko", detail="").body
    for reason in _REASONS:
        for lang in ("en", "ko"):
            body = needs_you_reason_body(reason, lang)
            assert body.strip(), f"{reason}/{lang} 본문이 비어 일반 fallback 으로 떨어진다"
        assert needs_you_reason_body(reason, "ko") != generic_ko
        assert needs_you_reason_body(reason, "ko") != needs_you_reason_body(reason, "en")
