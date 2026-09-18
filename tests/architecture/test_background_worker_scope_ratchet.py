"""배경 워커 스코프 래칫 — 새 워커가 가드 없이 들어올 수 없다 (#959).

#959 의 §1 을 닫는 과정에서 **전수 조사를 두 번 틀렸다**:

1. 처음엔 ``backend/workflow/infrastructure/workers/`` 디렉터리만 ``ls`` 했다.
   워커는 세 곳(``workflow/`` · ``knowledge/`` · ``schedule/``)에 흩어져 있고,
   하필 가장 큰 표면(``settle_worker``, ``workspace_id`` 참조 49개)을 놓쳤다.
2. 다음엔 ``BaseWorker`` 상속을 grep 했다. 실제로 루프를 도는 **러너**
   (``retraction_sweep``·``safe_mode_expiry``)는 ``BaseWorker`` 가 아니라
   ``ScheduleWorker`` 에 주입되는 객체라 또 빠졌다.

두 번 다 **내가 아는 위치만 증명**했다. 그래서 이 테스트는 위치도 상속도 아니라
**런타임이 실제로 띄우는 워커 집합**을 센다 — ``build_worker_runtime`` 이 돌려주는
그 목록이 프로덕션이 구동하는 것 자체다.

새 워커가 들어오면 이 핀이 깨지고, 작성자는 **스코프를 거는가, 아니면 왜 필요
없는가**를 아래 둘 중 하나에 명시해야 한다. 침묵으로 통과할 수 없다.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config import get_settings
from backend.workflow.application.runtime.worker_runtime import build_worker_runtime

#: 워커 목록은 설정에 따라 달라진다 — merge_watch 는 ``github_auto_merge_enabled``
#: 로 게이트된다. 래칫은 **최대 집합**을 세야 한다: 기본 설정으로 세면 게이트된
#: 워커가 조용히 빠지고, 그게 가드 없이 들어와도 아무것도 빨개지지 않는다.
#: (이 테스트가 처음 돌 때 정확히 그걸 잡았다 — merge_watch 가 핀엔 있는데
#: 목록엔 없었다.)
_GATE_ENV: dict[str, str] = {
    "BSVIBE_GITHUB_AUTO_MERGE_ENABLED": "true",
    # 워커를 구성만 하고 실행하지 않으므로 아무 32바이트 키면 된다(비밀 아님).
    "BSVIBE_GATEWAY_KMS_KEY_B64": base64.b64encode(b"\0" * 32).decode(),
}


# ---------------------------------------------------------------------------
# The pins. Adding a row is the explicit decision this guard exists to force.
# ---------------------------------------------------------------------------

#: 테넌트 행을 다루므로 반복마다 워크스페이스를 publish 해야 하는 워커.
#: 세션을 공유하면 ``workspace_session_scope``(GUC 까지), 반복마다 자기 트랜잭션을
#: 스코프 안에서 열면 ``workspace_scope``(contextvar 만) 로 충분하다.
SCOPED_WORKERS: frozenset[str] = frozenset(
    {
        "intake_worker",
        "agent_worker",
        "delivery_worker",
        "notify_worker",
        "daily_brief_worker",
        "auth_dependency_worker",
        "settle_worker",
        "merge_watch_worker",
        "schedule_worker",
        "safe_mode_expiry_worker",
        "retraction_sweep_worker",
    }
)

#: 스코프가 **필요 없는** 워커 — 각 행에 이유가 붙어야 한다. 이유는 전부 같은
#: 형태여야 한다: *그 워커가 만지는 표에 ``workspace_id`` 컬럼이 없다*. 그래서
#: 레이어 2(``_scoped_mappers()`` 가 그 컬럼으로 고른다)도 레이어 3(``_RLS_TABLES``)도
#: 애초에 대상이 아니다. "테넌트를 안 넘는다"는 주장만으로는 이 칸에 들어올 수 없다.
EXEMPT_WORKERS: dict[str, str] = {
    "relay_worker": "audit_outbox 에 workspace_id 컬럼이 없다 — 레이어 2·3 대상이 아니다",
    "audit_retention_sweep_worker": (
        "같은 audit_outbox 를 지운다. 워크스페이스 구분은 payload['workspace_id'] "
        "(JSON 필드)로 하는 명시적 술어이지 컬럼이 아니다"
    ),
}


@contextmanager
def _all_gates_open() -> Iterator[None]:
    """모든 게이트를 연 설정으로 잠깐 바꾼다.

    ⚠️ ``get_settings`` 는 lru_cache 다. 그리고 크리덴셜 cipher 는 넘겨받은
    settings 가 아니라 **자기가 직접** ``get_settings()`` 를 부른다(순환 import
    회피용 lazy import). 그래서 env 를 세우고 캐시를 비우는 것 말고는 방법이 없다.

    나갈 때 **다시 비운다** — 안 그러면 이 모듈이 세운 값이 뒤따르는 테스트
    모듈까지 따라간다(리포에 이미 기록된 함정:
    ``lru-cached-settings-pin-env-across-test-modules``).
    """
    prev = {k: os.environ.get(k) for k in _GATE_ENV}
    os.environ.update(_GATE_ENV)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def _runtime_worker_names() -> set[str]:
    """프로덕션이 실제로 띄우는 워커 이름 — 런타임 빌더에서 직접 뽑는다."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    sf: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    class _Stub:
        """Constructor 만 만족시키면 된다 — 어떤 워커도 여기서 돌지 않는다."""

        def __getattr__(self, _name: str) -> Any:
            async def _call(*_a: Any, **_k: Any) -> None:
                raise AssertionError("빌더는 워커를 실행하지 않아야 한다")

            return _call

    # ⚠️ 워커 목록은 **설정에 따라 달라진다** — merge_watch 는
    # ``github_auto_merge_enabled`` 로 게이트된다. 래칫은 **최대 집합**을 세야
    # 한다: 기본 설정으로 세면 게이트된 워커가 조용히 빠지고, 그 워커가 가드
    # 없이 들어와도 아무것도 빨개지지 않는다. (이 사실 자체를 이 테스트가
    # 처음 돌 때 잡았다 — merge_watch 가 핀에 있는데 목록엔 없었다.)
    with _all_gates_open():
        runtime = build_worker_runtime(
            session_factory=sf,
            execution=_Stub(),  # type: ignore[arg-type]
            delivery_adapter=_Stub(),  # type: ignore[arg-type]
            notify_sender=_Stub(),  # type: ignore[arg-type]
            settings=get_settings(),
        )
    return {w._name for w in runtime.workers}  # noqa: SLF001 — the registry is the subject


def test_every_running_worker_is_pinned() -> None:
    """런타임이 띄우는 워커는 전부 둘 중 한 칸에 있어야 한다."""
    running = _runtime_worker_names()
    pinned = SCOPED_WORKERS | set(EXEMPT_WORKERS)

    unpinned = sorted(running - pinned)
    assert not unpinned, (
        f"핀 없는 워커: {unpinned}. 테넌트 행을 다루면 SCOPED_WORKERS 에 넣고 루프에 "
        "워크스페이스 스코프를 걸어라. 아니면 EXEMPT_WORKERS 에 **왜 필요 없는지** "
        "(만지는 표에 workspace_id 컬럼이 없다) 적어라."
    )


def test_the_pins_do_not_rot() -> None:
    """사라진 워커가 핀에 남아 있으면 안 된다 — 낡은 핀은 거짓 안심을 준다."""
    running = _runtime_worker_names()
    stale = sorted((SCOPED_WORKERS | set(EXEMPT_WORKERS)) - running)
    assert not stale, f"이제 안 도는 워커가 핀에 남아 있다: {stale}"


def test_every_exemption_carries_a_reason() -> None:
    """면제는 이유가 있어야 한다. 빈 문자열은 침묵과 같다."""
    empty = sorted(name for name, why in EXEMPT_WORKERS.items() if not why.strip())
    assert not empty, f"이유 없는 면제: {empty}"


def test_the_census_can_actually_fail() -> None:
    """이 래칫이 빨개질 수 있는지 스스로 증명한다.

    핀을 하나 빼면 그 워커가 unpinned 로 잡혀야 한다. 이게 없으면
    ``_runtime_worker_names()`` 가 빈 집합을 돌려줘도 위 두 테스트는 통과한다.
    """
    running = _runtime_worker_names()
    assert running, "런타임에서 워커를 하나도 못 읽었다 — 이 래칫은 아무것도 재고 있지 않다"

    victim = next(iter(sorted(running)))
    pinned_without_victim = (SCOPED_WORKERS | set(EXEMPT_WORKERS)) - {victim}
    assert sorted(running - pinned_without_victim) == [victim], (
        "핀을 빼도 감지되지 않는다 — 이 래칫은 드리프트를 못 잡는다"
    )
