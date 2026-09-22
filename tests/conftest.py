"""Top-level pytest fixtures shared across the suite.

Goal: keep cross-test state from poisoning later tests. Specifically the
B16 / C2 :class:`backend.api.v1.live_events.LiveEventBus` is a process-wide
singleton; the SSE-redis-bus lift (C2) binds an :class:`asyncio.Redis`
client into it at app startup. In tests, ``create_app`` / ``run_workers``
get exercised within a per-test event loop — when that loop closes, the
singleton still holds a redis client tied to the now-dead loop, and the
next test's audit emit triggers a chain of "Task got Future attached to a
different loop" + "Event loop is closed" errors that escape via callbacks
into running tasks (mapped a Decision path to system_error in the executor
tests). The autouse fixture here resets the singleton state before AND
after each test so every test starts from a clean bus.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _isolate_w1_workspace_roots(tmp_path, monkeypatch) -> None:
    """W1: point product/run workspace roots at this test's ``tmp_path``.

    Without this, every test that goes through the create-product API path
    (or the new product workspace provisioner) would write to the host's
    ``var/products/`` and ``var/runs/`` — leaving FS detritus across runs
    and risking collisions on CI. The fixture is autouse + tmp_path-scoped
    so each test gets its own roots. Settings has ``model_config`` frozen?
    No — Settings is a Pydantic BaseSettings instance and ``monkeypatch.
    setattr`` works on it; the ``raising=False`` matters only if a future
    rename of either field drops the attribute.
    """
    from backend.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(
        settings, "product_workspace_root", str(tmp_path / "products"), raising=False
    )
    monkeypatch.setattr(settings, "run_workspace_root", str(tmp_path / "runs"), raising=False)


@pytest.fixture(autouse=True)
def _reset_live_event_bus_singleton() -> Iterator[None]:
    """Clear the process-wide ``LiveEventBus`` state between tests.

    The bus is an in-process fan-out keyed by ``workspace_id`` plus an
    optional Redis pub/sub leg. Stale state — leftover subscriber queues
    bound to closed event loops, stale relay tasks, an old redis client
    tied to a dead loop — must not leak across tests.
    """
    # Lazy import: not every test needs the bus module loaded.
    from backend.api.v1 import live_events as _le

    def _reset() -> None:
        bus = _le._BUS
        if bus is not None:
            bus._subscribers.clear()
            # Cancel any leftover relay tasks; ignore close errors since the
            # owning event loop may already be torn down.
            for task in list(bus._relay_tasks.values()):
                try:
                    task.cancel()
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass
            bus._relay_tasks.clear()
            bus._redis = None
        _le._BUS = None

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_oauth_rate_limit_buckets() -> Iterator[None]:
    """Clear the process-wide OAuth abuse-limit buckets between tests.

    The limiters in :mod:`backend.api.oauth` are module-level sliding-window
    counters — deliberately so (per-process, in-memory, no Redis in v1), which
    means they are shared by every test in the session. Without this, a module
    that drives ``/token`` or ``/device_authorization`` enough times leaves a
    full bucket behind and the NEXT module's first honest request answers 429
    for a reason that has nothing to do with what it is testing.
    """
    # Lazy import: not every test needs the OAuth module loaded.
    from backend.api.oauth import _reset_oauth_rate_limits_for_tests

    _reset_oauth_rate_limits_for_tests()
    yield
    _reset_oauth_rate_limits_for_tests()


# ---------------------------------------------------------------------------
# 구조화 로그 캡처 (#950 / #967 의 공통 선행작업)
# ---------------------------------------------------------------------------
# CI 가 애플리케이션 구조화 로그를 **아예** 안 잡고 있었다. #950 이 다섯 달 된
# flake 의 두 원인을 가르려고 워커 쪽 이벤트를 셌더니 0건이었는데, 음성 대조군
# ``event=`` 도 0건이었다 — 잴 수 없는 것을 "안 일어났다"로 읽으면 세 번째 오진이다.
#
# 두 겹이 겹쳐 있었다.
#   1. 아무도 ``configure_logging`` 을 안 부른다 ⇒ structlog 이 **설정되지 않은**
#      라이브러리 기본값(개발자 터미널용 ConsoleRenderer)으로 돈다.
#   2. pytest 가 stdout 을 삼킨다 ⇒ 통과한 테스트의 로그는 어디에도 안 남는다.
#      **그래서 stdout 이 아니라 파일로** 쓴다. CI 는 그 파일을 아티팩트로 올린다.
#
# 함수 스코프인 이유: 스위트 안에 자기 ``StringIO`` 로 structlog 을 재설정하는
# 테스트가 있다(``tests/shared/core/test_logging.py``). 세션 스코프로 한 번만
# 걸면 그 뒤의 모든 로그가 **닫힌 StringIO 로 조용히 사라진다** — 캡처가 죽어도
# 아무도 빨개지지 않는 형태라 가장 나쁘다. 매 테스트마다 되돌린다.
_TEST_LOG_ENV = "BSVIBE_TEST_LOG_FILE"
_TEST_LOG_LEVEL_ENV = "BSVIBE_TEST_LOG_LEVEL"


def _test_log_path() -> Path:
    """캡처 파일 경로 — 하드코딩하지 않고 환경변수로 받는다(기본값은 리포 ``var/``)."""
    override = os.environ.get(_TEST_LOG_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "var" / "test-logs" / "pytest-structlog.jsonl"


@pytest.fixture(scope="session")
def structlog_capture_path() -> Path:
    """이 런의 구조화 로그가 쌓이는 파일. 테스트가 직접 읽어 단언할 수 있다."""
    return _test_log_path()


@pytest.fixture(scope="session")
def _structlog_capture_file(structlog_capture_path: Path) -> Iterator[Any]:
    path = structlog_capture_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # 런마다 새로 시작한다 — 옛 줄이 딸려 오면 "변경 이후"를 셀 수 없다.
    handle = path.open("w", encoding="utf-8")
    try:
        yield handle
    finally:
        handle.flush()
        handle.close()


@pytest.fixture(autouse=True)
def _capture_structured_logs(_structlog_capture_file) -> None:
    """매 테스트 전에 structlog 을 그 파일로 되돌린다."""
    from backend.shared.core.logging import configure_logging

    configure_logging(
        level=os.environ.get(_TEST_LOG_LEVEL_ENV, "info"),
        json_output=True,
        service_name="pytest",
        stream=_structlog_capture_file,
    )
