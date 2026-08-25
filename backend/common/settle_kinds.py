"""Settle-payload ``kind`` 어휘 — 생산자와 소비자가 공유하는 단 하나의 출처.

``settle`` 활동의 ``payload["kind"]`` 는 **쓰는 쪽**(checkpoint 해소·Safe Mode 거절)과
**읽는 쪽**(SettleWorker 가 vault 로 흘린 뒤 ``NegativePatternRetriever`` 가 프론트매터에서
되읽음)이 문자열로 합의해야 하는 값이다. 그런데 그 문자열이 양쪽에 각각 리터럴로
박혀 있었다 — 한쪽만 바뀌면 조용히 안 맞는 모양이다.

여기 두는 이유는 하나 더 있다. 상수를 ``checkpoint_resolution`` 에서 가져오면
그 모듈의 의존 사슬(``plugin.audit.service`` → ``backend.extensions``,
``agent_runner`` → ``backend.router``)이 통째로 딸려와 **MCP 컨텍스트의 import 계약을
깬다** — import-linter 는 함수 안 지연 import 도 읽으므로 lazy import 로도 못 피한다.
``backend.common`` 은 아무것도 import 하지 않는 leaf 라 누구나 안전하게 의존한다.
"""

#: 형님이 거절한 접근 — "이건 하지 마라". ``NegativePatternRetriever`` 가 이 kind 의
#: 노트만 읽어 "avoid this" 가이드로 표면화한다.
NEGATIVE_PATTERN_SETTLE_KIND = "negative_pattern"

#: 형님이 해소한 Decision — 질문과 답이 함께 남아 ``ResolvedDecisionsRetriever`` 가 읽는다.
DECISION_RESOLUTION_SETTLE_KIND = "decision_resolution"


def founder_authored_text(
    *,
    answer: str | None,
    reason: str | None,
    action_key: str | None,
) -> str | None:
    """settlement 안에서 **형님이 직접 쓴 텍스트**만 골라낸다 — 없으면 ``None``.

    ``is_inherently_notable`` 이 두 kind 에 부여하는 *"LLM 판단 없이 무조건 기억가치
    있음"* 의 **전제**가 바로 이것이다: *"a user decision or a discard-with-reason is
    knowledge by construction"* — 형님이 실제로 무언가를 썼다. 그런데 그 전제는 생산자
    한쪽(``SafeModeQueue.deny`` 의 ``if flipped and reason_text``)에만 있었고
    ``resolve_checkpoint`` 는 무조건 기록했다. 같은 규칙, 두 생산자, 한쪽만 준수 →
    prod 실측 ``decision_resolution`` 11건 중 **6건(55%)이 형님이 쓴 글자 0자**.

    그래서 규칙을 여기 leaf 에 **한 번** 적는다. ``backend.common`` 은 아무것도 import
    하지 않으므로 생산자(workflow)·소비자(knowledge) 어느 쪽에서도 안전하게 의존한다.

    판정:

    * ``reason`` — 형님이 친 거절 사유. 있으면 그것이 근거다.
    * ``action_key`` 가 있으면 ``answer`` 는 **버튼 키 그 자체**다
      (``resolve_checkpoint``: ``resolution_text = action_key if action_key is not None
      else answer``). 원클릭 액션은 형님이 친 글자가 0자이므로 근거가 못 된다. 액션
      이름을 열거하지 않는 것이 요점이다 — 다음에 추가될 원클릭 액션도 그냥 걸린다.
    * 그 외의 ``answer`` — 형님이 직접 친 문장.

    빈 문자열/공백만은 없는 것과 같다: 비어 있음을 기록된 판단으로 오독하면 안 된다
    (``deny`` 가 blank reason 을 NULL 로 저장하는 것과 같은 규율).
    """
    written = (reason or "").strip()
    if written:
        return written
    if action_key is not None:
        return None
    written = (answer or "").strip()
    return written or None


__all__ = [
    "DECISION_RESOLUTION_SETTLE_KIND",
    "NEGATIVE_PATTERN_SETTLE_KIND",
    "founder_authored_text",
]
