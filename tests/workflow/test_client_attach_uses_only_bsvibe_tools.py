"""``client_attach`` 도 BSVibe MCP 툴만 쓴다 — 네이티브 툴은 절대 안 된다.

형님 판정 2026-08-24: *"client_attach도 bsvibe mcp tool만 써야 해. worker랑
client가 동일하다는 보장이 없잖아. 네이티브 툴은 절대 사용해서는 안돼."*

이 원칙은 새 것이 아니다 — ``backend/dispatch/adapter.py`` 가 이미 적어두고 있었다:

    *"the executor is the user's LLM CLIENT, not an execution environment …
    an agent must act through BSVibe's tools, executed server-side — not through
    the CLI's own local tools in a temp dir the worker scrapes back. That old
    shape is what let an agent invent a codebase in an empty dir and ship it,
    zero a >256 KB file, and lose an entire result on a deletion."*

#692 가 ``client_attach`` 에만 그 옛 모양을 되살려 놨다: 워크툴 5종(``file_*`` /
``shell_exec``)을 뺏고 ``native_tools=True`` 로 CLI 자기 손을 쓰게 했다.

**실측된 결과 (prod, BStockReport 주간 런 ``53f2cbce``, 2026-08-24 00:30 UTC):**
CLI 분기가 ``--allowedTools "<플랫폼 4개>" --permission-mode acceptEdits`` 였다.
``acceptEdits`` 는 편집만 자동승인하고 **Bash 는 승인하지 않는다.** 네이티브 툴은
allowlist 에도 없고 헤드리스라 승인 프롬프트도 뜰 수 없다 → 셸 실행 경로가 0개.
그 제품의 작업 전체가 ``uv run bstockreport run --emit`` 셸 한 줄이다.
결과: 1 llm_turn · 0 tool_call · **0 딜리버러블**. (직전 4회는 2/3/10/14개.)

고치는 방법은 표면을 넓히는 것이 아니라 **결정 지점을 없애는 것**이다: 표면은 하나고,
``client_attach`` 의 워크툴은 ``ClientWorkerSandboxSession`` 을 통해 파운더의 머신에서
돈다 — in-place 검증 게이트가 이미 그 기제로 돌고 있다. 서버는 소스를 clone·저장하지
않으므로 소스 노출 금지 계약도 그대로다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio

_WORKER_ID = uuid.uuid4()
_WORKSPACE_HALF = ("file_read", "file_write", "file_edit", "file_list", "shell_exec")


def test_there_is_one_tool_surface_not_one_per_execution_model() -> None:
    """표면을 실행모델로 가르는 seam 자체가 사라진다 — 결정 지점 0개."""
    from backend.workflow.application import tool_registry

    absent = [
        n for n in ("mcp_tool_names_for", "PLATFORM_TOOL_MCP_NAMES") if hasattr(tool_registry, n)
    ]
    assert not absent, f"실행모델별 표면 분기가 아직 있다: {absent}"


def test_the_whole_work_surface_survives() -> None:
    """양성 대조군 — 표면 자체는 온전하다. 지운 것은 '가르는 축'뿐이다."""
    from backend.workflow.application.tool_registry import WORK_TOOL_MCP_NAMES

    for tool in _WORKSPACE_HALF:
        assert f"bsvibe_work_{tool}" in WORK_TOOL_MCP_NAMES
    assert "bsvibe_work_emit_deliverable" in WORK_TOOL_MCP_NAMES


def test_the_cli_never_keeps_its_own_hands() -> None:
    """``native_tools`` 라는 개념이 배선 전체에서 사라진다.

    dispatch entry → 워커 페이로드 → CLI 인자, 세 곳 모두. 하나라도 남으면
    ``client_attach`` 런이 다시 CLI 자기 손으로 돌 수 있다."""
    import inspect

    from backend.dispatch import adapter
    from backend.executors import dispatch as executor_dispatch
    from backend.executors.worker import claude_code

    leaks = [
        mod.__name__
        for mod in (adapter, executor_dispatch, claude_code)
        if "native_tools" in inspect.getsource(mod)
    ]
    assert not leaks, f"native_tools 가 아직 살아 있다: {leaks}"


async def test_client_attach_work_tools_run_on_the_founders_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """핵심 — 워크툴을 주는 것만으로는 부족하다. **옳은 머신**에서 돌아야 한다.

    MCP 트랜스포트가 서버 DinD 싱글턴으로만 샌드박스를 해결하면 client_attach 런의
    ``shell_exec`` 는 소스가 없는 **서버 워크트리**에서 돈다."""
    from backend.mcp.tools import work_registry

    client = _install_fakes(monkeypatch, tmp_path, "client_attach", "/home/founder/proj")
    registry = await _registry(work_registry, tmp_path)
    out = await registry.invoke("shell_exec", {"command": "echo hi"})

    assert client["built"], "클라이언트 워커 샌드박스가 만들어지지 않았다 — 서버에서 돌았다"
    assert client["dir"] == "/home/founder/proj"
    # 머신은 런 자신의 디스패치된 태스크에서 나온다 — 에이전트가 실제로 도는 그 박스.
    assert client["pin"] == _WORKER_ID
    assert client["executor_type"] == "claude_code"
    assert client["exec"] == ["echo hi"]
    assert "hi" in out


async def test_server_sandbox_run_still_gets_the_server_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """양성 대조군 — 다른 실행모델은 손대지 않는다. 이 테스트는 변경 전에도 green 이다."""
    from backend.mcp.tools import work_registry

    client = _install_fakes(monkeypatch, tmp_path, "server_sandbox", None)
    registry = await _registry(work_registry, tmp_path)
    out = await registry.invoke("shell_exec", {"command": "echo hi"})

    assert not client["built"], "서버 런이 파운더 머신으로 나갔다"
    assert client["server_exec"] == ["echo hi"]
    assert "hi" in out


class _Box:
    """SandboxSession 최소 구현 — 어느 머신에서 돌았는지만 기록한다."""

    def __init__(self, log: list[str], *, in_place: bool) -> None:
        self._log = log
        self.runs_in_place = in_place

    async def exec(self, command: str, *, timeout_s: float = 0.0, shell: bool = False) -> Any:
        self._log.append(command)
        return SimpleNamespace(exit_code=0, stdout="hi", stderr="", timed_out=False)


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: str, client_dir: str | None
) -> dict[str, Any]:
    """두 샌드박스 백엔드를 모두 가짜로 세우고, 어느 쪽이 쓰였는지 남긴다."""
    from backend.mcp.tools import work_registry
    from backend.workers import emit as workers_emit
    from backend.workflow.application.runtime import sandbox_selection
    from backend.workflow.infrastructure.sandbox import client_worker_manager

    seen: dict[str, Any] = {"built": False, "dir": None, "exec": [], "server_exec": []}

    class _ClientMgr:
        def __init__(self, **kw: Any) -> None:
            seen["built"] = True
            seen["dir"] = kw.get("client_workspace_dir")
            seen["pin"] = kw.get("pinned_worker_id")
            seen["executor_type"] = kw.get("executor_type")

        async def acquire(self, *_a: Any, **_k: Any) -> Any:
            return _Box(seen["exec"], in_place=True)

    class _ServerMgr:
        async def acquire(self, *_a: Any, **_k: Any) -> Any:
            return _Box(seen["server_exec"], in_place=False)

    monkeypatch.setattr(client_worker_manager, "ClientWorkerSandboxManager", _ClientMgr)
    monkeypatch.setattr(work_registry, "get_sandbox_manager", lambda: _ServerMgr())
    monkeypatch.setattr(
        work_registry, "run_worktree_path", lambda rid: tmp_path / "runs" / str(rid)
    )
    monkeypatch.setattr(
        sandbox_selection, "product_dispatch_config", _fake_dispatch_config(target, client_dir)
    )
    monkeypatch.setattr(workers_emit, "get_dispatch_redis_client", lambda _s: object())
    return seen


async def _registry(work_registry: Any, tmp_path: Path) -> Any:
    from backend.mcp.api import McpPrincipal, ToolContext
    from backend.workflow.application.mcp_work_effects import resolve_client_sandbox

    run = _Run()
    (tmp_path / "runs" / str(run.id)).mkdir(parents=True, exist_ok=True)
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
        # 프로덕션 합성 루트가 넣어주는 리졸버 — 여기서도 진짜를 쓴다.
        extras={"client_sandbox": resolve_client_sandbox},
        # 프로덕션이 실제로 채우는 값 (``backend/mcp/server.py`` 의 디스패처) — 이걸
        # 안 주면 client_attach 분기가 "컨텍스트 불완전"으로 조용히 서버 박스로 내려간다.
        session_factory=_Session(run),
    )
    return await work_registry.build_run_tool_registry(run.id, ctx)


class _Run:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {}


class _Session:
    """런 조회 + "이 런이 도는 워커" 조회 두 가지만 하는 최소 세션."""

    def __init__(self, run: _Run) -> None:
        self._run = run

    async def get(self, _model: Any, _pk: Any) -> Any:
        return self._run

    async def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(first=lambda: (_WORKER_ID, "claude_code"))

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def __call__(self) -> _Session:
        return self


def _fake_dispatch_config(target: str, client_dir: str | None) -> Any:
    async def _cfg(_session: Any, _pid: Any) -> tuple[str | None, str, str | None]:
        return None, target, client_dir

    return _cfg


async def test_the_machine_is_the_one_this_run_is_actually_running_on() -> None:
    """실 DB — "어느 머신인가"는 런의 디스패치된 태스크가 답한다.

    워커가 죽어 런이 재디스패치되면 **지금** 도는 박스를 가리켜야 한다. 죽은 워커는
    이 런의 트리를 만진 적조차 없다. 그래서 최신 행이다.

    ``worker_id`` 가 아직 없는 행(미디스패치)은 답이 될 수 없다 — 그 태스크는 어떤
    머신에도 놓이지 않았다.
    """
    import backend.executors.db  # noqa: F401  — Base.metadata 에 executor 테이블 등록
    from backend.executors.db import ExecutorTaskRow
    from backend.workflow.application.runtime.sandbox_selection import _worker_running_run
    from tests._support import memory_session

    run_id, ws = uuid.uuid4(), uuid.uuid4()
    dead, live = uuid.uuid4(), uuid.uuid4()

    async with memory_session() as session:
        for idx, (worker, kind) in enumerate(
            ((dead, "claude_code"), (live, "claude_code"), (None, "claude_code"))
        ):
            session.add(
                ExecutorTaskRow(
                    workspace_id=ws,
                    run_id=run_id,
                    worker_id=worker,
                    executor_type=kind,
                    prompt=f"turn {idx}",
                    created_at=datetime(2026, 8, 24, 0, idx, tzinfo=UTC),
                )
            )
        session.add(
            ExecutorTaskRow(
                workspace_id=ws,
                run_id=uuid.uuid4(),
                worker_id=uuid.uuid4(),
                executor_type="claude_code",
                prompt="another run",
                created_at=datetime(2026, 8, 24, 9, 0, tzinfo=UTC),
            )
        )
        await session.flush()

        found = await _worker_running_run(session, run_id)

    assert found is not None, "디스패치된 태스크가 있는데 머신을 못 찾았다"
    assert found == (live, "claude_code")


async def test_a_run_that_never_reached_a_machine_gets_no_client_box() -> None:
    """양성 대조군 — 미디스패치 런은 조용히 잘못된 머신으로 가지 않고 ``None`` 이다."""
    import backend.executors.db  # noqa: F401
    from backend.workflow.application.runtime.sandbox_selection import _worker_running_run
    from tests._support import memory_session

    async with memory_session() as session:
        assert await _worker_running_run(session, uuid.uuid4()) is None


def test_the_composition_root_actually_injects_the_client_box() -> None:
    """주입이 빠지면 client_attach 런은 **조용히** 서버 박스로 내려간다 — 에러 없이.

    그 실패는 소스가 없는 트리에서 툴이 거부되는 모습으로만 드러난다. 배선 자체를 잡는다."""
    import inspect

    from backend.api import main
    from backend.mcp import lifespan, server

    assert "client_sandbox" in inspect.signature(server.build_server).parameters
    assert "client_sandbox" in inspect.signature(lifespan.mcp_lifespan).parameters
    assert "client_sandbox=resolve_client_sandbox" in inspect.getsource(main)
