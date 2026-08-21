"""제품 bootstrap 수명주기 어휘 — 쓰는 쪽과 읽는 쪽이 공유하는 단 하나의 출처.

이 상수들은 원래 ``workflow/application/runtime/product_bootstrap_runtime.py`` 에
선언돼 있었고, 그 이유까지 적혀 있었다 —

    *"Lifecycle vocabulary — kept here so the API surface + the PWA can agree on
    the exact strings **without a free-string drift**."*

그런데 **그 상수를 import 하는 곳이 0곳**이었다. 대신 "이 제품이 아직 도는 중인가"를
판정하는 집합이 ``frozenset({"pending", "cloning", "analyzing", "ingesting"})`` 로
**바이트 동일하게 두 벌** 있었다 (REST 취소/재시도 · MCP 취소/재시도). SoT 가 없어서가
아니라 **있는데 안 쓴** 문제였다.

여기 두는 이유는 :mod:`backend.common.settle_kinds` 와 같다 — ``product_bootstrap_runtime``
은 의존 사슬이 무거워서, 거기서 상수를 가져오면 그 사슬이 통째로 딸려와 **MCP 컨텍스트의
import 계약을 깬다.** ``backend.common`` 은 아무것도 import 하지 않는 leaf 다.

⚠️ **이 문자열들은 ``products.bootstrap_status`` 에 이미 쌓여 있다.** 값을 바꾸면 기존
행의 의미가 달라진다 — 여기서 바꿀 것은 *어디서 읽는가*이지 *무엇인가*가 아니다.
"""

from __future__ import annotations

STATUS_PENDING = "pending"
STATUS_CLONING = "cloning"
STATUS_ANALYZING = "analyzing"
STATUS_INGESTING = "ingesting"
STATUS_COMPLETE = "complete"

#: #692 — 제품이 형님의 **자기 머신**에서 도는 경우. BSVibe 가 서버에서 소스를
#: clone 하거나 ingest 하지 않는다. ``complete``(아무것도 ingest 안 됨)도
#: ``failed``(잘못된 것 없음)도 아닌, 정직한 제3의 종착점이다.
STATUS_SKIPPED_CLIENT_ATTACH = "skipped:client_attach"

STATUS_FAILED_CLONE = "failed:clone"
STATUS_FAILED_TOO_LARGE = "failed:too_large"
STATUS_FAILED_INGEST = "failed:ingest"

#: 아직 도는 중 — 취소/재시도 표면이 "지금 끼어들어도 되나"를 이걸로 판정한다.
#: 종착 상태(``complete`` · ``skipped:*`` · ``failed:*``)는 여기 없다.
IN_FLIGHT_STATUSES = frozenset({STATUS_PENDING, STATUS_CLONING, STATUS_ANALYZING, STATUS_INGESTING})

__all__ = [
    "IN_FLIGHT_STATUSES",
    "STATUS_ANALYZING",
    "STATUS_CLONING",
    "STATUS_COMPLETE",
    "STATUS_FAILED_CLONE",
    "STATUS_FAILED_INGEST",
    "STATUS_FAILED_TOO_LARGE",
    "STATUS_INGESTING",
    "STATUS_PENDING",
    "STATUS_SKIPPED_CLIENT_ATTACH",
]
