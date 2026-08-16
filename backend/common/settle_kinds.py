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

__all__ = ["DECISION_RESOLUTION_SETTLE_KIND", "NEGATIVE_PATTERN_SETTLE_KIND"]
