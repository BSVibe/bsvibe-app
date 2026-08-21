"""확장 Protocol 스텁이 ``runtime_checkable`` 인지.

Lift G 는 훅 표면 넷을 "구현 0개"로 발행했다. 그중 pre-dispatch 인터셉터와
settlement 구독자 둘은 끝내 구현을 얻지 못했고 2026-08-21 에 지웠다 —
이 파일의 ``test_hook_protocol_has_zero_registered_impl`` 이 **"아무도 구현하지
않음"을 계약으로 박아두고 있었다.** 발행-미사용 상태를 명세로 고정하면 그 상태가
영원해진다.

남은 것은 실제로 쓰이는 표면이다:

* ``EventBus`` + ``EventBusSubscriber`` — pub/sub Protocols. 구독자 등록이
  아직 0이라 zero-registration 검사는 이쪽에만 남긴다.
* ``Plugin`` / ``Skill`` / ``Action`` — 플러그인/스킬 로더가 이미 만들어내는 것을
  형식화한다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol

import pytest

from backend.extensions.domain import protocols


@pytest.mark.parametrize(
    "name",
    [
        "EventBus",
        "EventBusSubscriber",
        "Plugin",
        "Skill",
        "Action",
    ],
)
def test_protocol_is_runtime_checkable(name: str) -> None:
    proto = getattr(protocols, name)
    assert issubclass(proto, Protocol), f"{name} must be a Protocol subclass"
    # runtime_checkable Protocols expose _is_runtime_protocol = True
    assert getattr(proto, "_is_runtime_protocol", False), (
        f"{name} must be marked @runtime_checkable"
    )


@pytest.mark.parametrize(
    "name",
    ["EventBusSubscriber"],
)
def test_hook_protocol_has_zero_registered_impl(name: str) -> None:
    """Lift G publishes hook surfaces but does not wire any concrete impl.

    Grep the entire backend tree for ``register_<hook>`` — must return 0
    hits. (Test files referencing the name are allowed; production code is
    not. We scope the grep to ``backend/``.)
    """
    repo_root = Path(__file__).resolve().parents[3]
    backend = repo_root / "backend"
    # snake_case the hook name for register_<snake>(...) lookups.
    snake = "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")
    pattern = f"register_{snake}"
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-l", pattern, str(backend)],
        capture_output=True,
        text=True,
        check=False,
    )
    hits = [line for line in result.stdout.splitlines() if line.strip()]
    assert hits == [], f"Lift G expects zero live registrations of {pattern}; found: {hits}"
