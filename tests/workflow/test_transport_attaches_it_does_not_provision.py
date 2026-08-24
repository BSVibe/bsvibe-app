"""MCP 트랜스포트는 박스에 **붙기만** 한다 — 프로비저닝은 런 수명주기의 일이다.

#813 에서 내가 넣은 회귀. MCP 워크툴 트랜스포트는 **툴 호출마다** 박스를 다시
해결하는데, 거기서 ``ClientWorkerSandboxManager.acquire`` 를 불렀다. 그런데
``acquire`` 는 파운더 머신으로 **exec 태스크 2건**을 디스패치한다 — 워크트리
프로비저닝과 고아 워크트리 청소.

``acquire`` 자신의 docstring 이 답을 갖고 있었다:

    *"Here specifically because ``acquire`` already runs BEFORE the drive loop
    dispatches the first agent turn"*

즉 **프로비저닝은 런당 한 번**, 오케스트레이터가 이미 한다. 트랜스포트가 그것을
반복하면:

* 툴 호출 1건당 워커 왕복 2회 — 에이전트가 툴 20번 쓰면 왕복 40회가 덧붙는다
* **고아 워크트리 청소가 런당 1회가 아니라 툴 호출마다** 돈다. 동시에 시작하는
  다른 런의 체크아웃을 지울 노출이 그만큼 커진다

서버 쪽에서 정확히 같은 부류를 ``tests/mcp/test_sandbox_reuse.py`` 가 막고 있다
(E1: 툴 호출마다 컨테이너를 부수고 다시 만들던 결함). 이 모듈은 그 성질을 클라이언트
백엔드에 세운다.

프로비저닝이 멱등이라 **손상은 없었다** — 비용과 노출이 문제다. 그래서 이 가드는
"고쳐졌나"가 아니라 **"트랜스포트가 디스패치 기질을 아예 필요로 하지 않는가"** 를
묻는다: 붙기만 한다면 워커가 없어도 레지스트리는 나와야 한다.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

_CLIENT_DIR = "/home/founder/proj"


async def test_building_the_registry_dispatches_nothing_to_the_founders_machine(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """레지스트리를 만드는 데 파운더 머신으로 나가는 명령이 0건이어야 한다.

    exec 을 직접 센다 — 프로비저닝이든 청소든 전부 ``exec`` 을 거친다."""
    from backend.mcp.tools import work_registry
    from backend.workflow.domain.client_worktree import client_run_worktree
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxSession,
    )

    sent: list[str] = []

    async def _record(self: Any, command: str, **_kw: Any) -> Any:
        sent.append(command)
        return _Result()

    monkeypatch.setattr(ClientWorkerSandboxSession, "exec", _record)
    run = _Run()
    _patch_config(monkeypatch, tmp_path, redis=object())
    registry = await _registry(work_registry, run)

    assert sent == [], f"트랜스포트가 파운더 머신으로 명령을 보냈다: {sent}"
    box = registry._sandbox
    assert box is not None, "client_attach 런이 박스를 못 받았다"
    assert box.runs_in_place is True
    assert box.workspace_mount == client_run_worktree(_CLIENT_DIR, run.id)


async def test_attaching_never_execs(monkeypatch: pytest.MonkeyPatch) -> None:
    """매니저 계약 — ``attach`` 는 명령을 하나도 보내지 않는다."""
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxSession,
    )

    run_id = uuid.uuid4()
    sent: list[str] = []

    async def _record(self: Any, command: str, **_kw: Any) -> Any:
        sent.append(command)
        raise AssertionError("attach 가 exec 을 보냈다")

    monkeypatch.setattr(ClientWorkerSandboxSession, "exec", _record)
    manager = _manager(run_id, redis=None)

    box = manager.attach()

    assert sent == []
    assert box.runs_in_place is True


async def test_the_run_lifecycle_still_provisions(monkeypatch: pytest.MonkeyPatch) -> None:
    """양성 대조군 — ``acquire`` 는 그대로 프로비저닝한다. 이 PR 은 그것을 옮기지 않는다.

    옮겼다면 워크트리를 만드는 곳이 아무 데도 없어지고, 에이전트의 첫 턴은 존재하지
    않는 디렉터리에서 시작한다."""
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxSession,
    )

    run_id = uuid.uuid4()
    sent: list[str] = []

    async def _ok(self: Any, command: str, **_kw: Any) -> Any:
        sent.append(command)
        return _Result()

    monkeypatch.setattr(ClientWorkerSandboxSession, "exec", _ok)
    manager = _manager(run_id, redis=object())

    await manager.acquire(uuid.uuid4(), "/app/var/runs/whatever")

    assert sent, "acquire 가 워크트리를 프로비저닝하지 않았다"
    assert any("worktree" in c for c in sent)


class _Result:
    exit_code = 0
    stdout = ""
    stderr = ""
    timed_out = False


def _manager(run_id: uuid.UUID, *, redis: Any) -> Any:
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxManager,
    )

    return ClientWorkerSandboxManager(
        redis=redis,
        session_factory=_Session(None),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        pinned_worker_id=uuid.uuid4(),
        default_timeout_s=30.0,
        client_workspace_dir=_CLIENT_DIR,
        run_id=run_id,
    )


class _Run:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {}


class _Session:
    def __init__(self, run: _Run | None) -> None:
        self._run = run

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self._run

    async def execute(self, stmt: Any) -> Any:
        from types import SimpleNamespace

        # 이 페이크는 두 종류 질문을 받는다 — "이 런은 어느 워커에서 도나"와
        # "이 제품이 선언한 시크릿은". 묻는 것에 맞게 답한다.
        if "executor_tasks" in str(stmt):
            return SimpleNamespace(first=lambda: (uuid.uuid4(), "claude_code"))
        # 제품 메타데이터 — 이 제품은 시크릿을 선언하지 않았다.
        return SimpleNamespace(first=lambda: ({},))

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def __call__(self) -> _Session:
        return self


def _patch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, *, redis: Any) -> None:
    from backend.mcp.tools import work_registry
    from backend.workers import emit as workers_emit
    from backend.workflow.application.runtime import sandbox_selection

    async def _cfg(_session: Any, _pid: Any) -> tuple[str | None, str, str | None]:
        return None, "client_attach", _CLIENT_DIR

    monkeypatch.setattr(sandbox_selection, "product_dispatch_config", _cfg)
    monkeypatch.setattr(workers_emit, "get_dispatch_redis_client", lambda _s: redis)
    monkeypatch.setattr(
        work_registry, "run_worktree_path", lambda rid: tmp_path / "runs" / str(rid)
    )


async def _registry(work_registry: Any, run: _Run) -> Any:
    from backend.mcp.api import McpPrincipal, ToolContext
    from backend.workflow.application.mcp_work_effects import resolve_client_sandbox

    ctx = ToolContext(  # type: ignore[arg-type]
        principal=McpPrincipal(
            user_id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            client_id="bsvibe-worker",
            scopes=frozenset({"mcp:read", "mcp:write"}),
            jti=uuid.uuid4(),
            run_id=run.id,
        ),
        session=_Session(run),
        session_factory=_Session(run),
        extras={"client_sandbox": resolve_client_sandbox},
    )
    return await work_registry.build_run_tool_registry(run.id, ctx)
