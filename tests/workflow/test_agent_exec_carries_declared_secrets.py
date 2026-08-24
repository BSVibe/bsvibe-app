"""에이전트가 돌리는 명령도 제품이 선언한 시크릿을 받는다.

**형님 판정 2026-08-24**: *"env를 그냥 서버에서 일괄관리하는게 가장 깔끔하려나"* → 응.

기제는 이미 있었고, 심지어 client_attach 를 위해 지어져 있었다:

    products.product_metadata (봉인)
        → verify_environment 가 **마지막 순간에만** 복호화, 저장 안 함
        → 디스패치 채널 → 파운더 머신

빠진 것은 그 흐름이 **검증 스택에만** 걸려 있었다는 것이다. 두 가지가 새어나간다:

* **에이전트의 ``shell_exec``** — exec 디스패치가 ``env_names=[]`` 로 나갔다
* **``kind="host"`` 게이트 명령** — client_attach 는 컨테이너가 없어 스택 플랜이
  ``None`` 이고(``StackNotApplicable``), 그러면 명령이 박스에서 **직접** 돈다.
  시크릿을 boot 명령에만 붙이는 컨테이너 경로를 아예 타지 않는다

그래서 이건 누락이 아니라 **파리티 결함**이었다: 게이트가 받는 환경과 에이전트가 받는
환경이 다르면, **에이전트는 게이트가 돌릴 명령을 스스로 재현할 수 없다.**

실측 (BStockReport, 2026-08-24): 런 워크트리는 `.env` 가 없고(gitignored → git worktree 가
tracked 파일만 가져온다) exec 은 ``env_names=[]`` 였다. 파운더 체크아웃에서 같은 명령은
진짜 리포트를 낸다. 워크트리에서는 *"API 키가 비어 있어요"* 만 나온다.

∴ 시크릿은 **박스가** 들고 다닌다. 호출 지점마다 뿌리면 두 번째 소스가 되고, 덜 쓰이는
쪽으로 갈라진다 ([[mirrored-surface-drifts-in-the-direction-of-least-testing]]).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


async def test_the_agents_command_carries_the_declared_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """박스가 시크릿을 들고 다닌다 — 호출자는 아무것도 안 넘겨도 된다.

    ``ToolRegistry._shell_exec`` 은 ``env`` 를 넘기지 않는다. 그래도 도착해야 한다."""
    sent = _patch_dispatch(monkeypatch)
    box = _session(secrets={"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})

    await box.exec("uv run bstockreport run --emit", timeout_s=30.0, shell=True)

    assert sent["env"] == {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"}


async def test_a_product_that_declares_nothing_takes_the_old_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """양성 대조군 — 선언이 없으면 예전과 글자 그대로 같은 경로다."""
    sent = _patch_dispatch(monkeypatch)
    box = _session(secrets=None)

    await box.exec("pytest -q", timeout_s=30.0, shell=True)

    assert not sent["env"]


async def test_a_callers_own_env_wins_over_the_boxs(monkeypatch: pytest.MonkeyPatch) -> None:
    """검증 스택은 boot 명령에 자기 env 를 붙인다. 그 의도가 박스 기본값에 먹히면 안 된다."""
    sent = _patch_dispatch(monkeypatch)
    box = _session(secrets={"TOKEN": "from-product", "KEEP": "yes"})

    await box.exec("docker compose up", timeout_s=30.0, shell=True, env={"TOKEN": "from-caller"})

    assert sent["env"] == {"TOKEN": "from-caller", "KEEP": "yes"}


async def test_secrets_never_enter_the_command_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """값은 env 로만 간다 — 명령 문자열은 로그·에러·프로세스 목록에 남는다."""
    sent = _patch_dispatch(monkeypatch)
    box = _session(secrets={"ALPACA_SECRET_KEY": "super-secret-value"})

    await box.exec("uv run bstockreport run", timeout_s=30.0, shell=True)

    assert "super-secret-value" not in sent["command"]


async def test_the_resolver_unseals_what_the_product_declared() -> None:
    """선언 → 봉인 → 복호화가 왕복한다. 검증이 쓰는 그 함수 하나를 그대로 쓴다."""
    from backend.workflow.application.runtime.sandbox_selection import (
        declared_secrets_for_metadata,
    )
    from backend.workflow.domain.verify_secrets import seal_secrets

    # ``seal_secrets`` adds the ``enc:`` marker itself and ``unseal_secrets`` strips it,
    # so the fake cipher is just a reversible transform — a true inverse pair.
    sealed = seal_secrets(
        {"verify_secrets": {"ALPACA_API_KEY": "plain"}}, encrypt=lambda s: s[::-1], prior={}
    )
    assert "plain" not in str(sealed), "봉인 후에도 평문이 남아 있다"
    out = declared_secrets_for_metadata(sealed, decrypt=lambda s: s[::-1])

    assert out == {"ALPACA_API_KEY": "plain"}


async def test_the_resolved_box_carries_what_the_product_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """배선 가드 — 선언된 시크릿이 **실제로 박스까지** 도달한다.

    ⚠️ 이 테스트가 없을 때 배선을 ``env=None`` 으로 끊어봤더니 **아무 테스트도 안 깨졌다.**
    세션 동작만 덮고 제품→박스 경로를 안 덮고 있었다
    ([[unit-test-supplies-what-production-withholds]])."""
    from backend.router.accounts.crypto import CredentialCipher
    from backend.workflow.application.runtime import sandbox_selection
    from backend.workflow.domain.verify_secrets import METADATA_KEY, seal_secrets

    # 키 FUNCTION 만 갈아끼운다 — 설정 캐시를 건드리면 세션 전체가 오염된다
    # (test_verify_secret_transport.py 의 kms_key 픽스처가 같은 이유로 이렇게 한다).
    key = b"0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(
        "backend.router.accounts.crypto._key_from_settings", lambda: key, raising=True
    )
    sealed = seal_secrets(
        {METADATA_KEY: {"ALPACA_API_KEY": "plain"}}, encrypt=CredentialCipher(key).encrypt
    )

    box = await sandbox_selection.client_sandbox_manager_for_run(
        session=_ProductSession(metadata=sealed),
        run_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        redis_client=object(),
        session_factory=_Factory(),
        workspace_id=uuid.uuid4(),
        timeout_s=30.0,
    )

    assert box is not None, "client_attach 제품인데 박스를 못 받았다"
    assert box.attach()._env == {"ALPACA_API_KEY": "plain"}


class _ProductSession:
    """세 가지 질문에 답한다 — 어느 워커냐 · 디스패치 설정 · 선언된 시크릿."""

    def __init__(self, *, metadata: dict[str, Any]) -> None:
        self._metadata = metadata

    async def execute(self, stmt: Any) -> Any:
        from types import SimpleNamespace

        text = str(stmt)
        if "executor_tasks" in text:
            return SimpleNamespace(first=lambda: (uuid.uuid4(), "claude_code"))
        if "repo_url" in text:  # product_dispatch_config
            return SimpleNamespace(
                first=lambda: (
                    None,
                    {
                        "execution_target": "client_attach",
                        "client_workspace_path": "/home/founder/proj",
                        **self._metadata,
                    },
                )
            )
        return SimpleNamespace(first=lambda: (self._metadata,))


def test_the_loop_seam_threads_secrets_to_the_box() -> None:
    """루프 경로의 공유 seam — 넘긴 시크릿이 박스까지 간다."""
    from backend.workflow.application.runtime.sandbox_selection import sandbox_manager_for_run

    manager = sandbox_manager_for_run(
        default=object(),  # type: ignore[arg-type]
        execution_target="client_attach",
        client_workspace_dir="/home/founder/proj",
        account=_Account(),
        redis_client=object(),
        session_factory=_Factory(),
        workspace_id=uuid.uuid4(),
        timeout_s=30.0,
        run_id=uuid.uuid4(),
        secrets={"ALPACA_API_KEY": "plain"},
    )

    assert manager.attach()._env == {"ALPACA_API_KEY": "plain"}  # type: ignore[union-attr]


def test_the_loop_factory_actually_resolves_them() -> None:
    """배선 가드 — 팩토리가 실제로 시크릿을 넘긴다.

    ⚠️ 이 가드 없이 ``secrets=None`` 으로 끊어봤더니 **1440개가 전부 통과했다.**
    이 팩토리는 의존이 무거워 동작으로 세우기 어렵다. 그렇다고 안 재면 배선이 조용히
    끊긴다 — client_attach 런은 에러 없이 시크릿 없는 박스를 받는다."""
    import inspect

    from backend.workflow.application.runtime import agent_runtime

    src = inspect.getsource(agent_runtime)
    assert "secrets=(" in src, "팩토리가 sandbox_manager_for_run 에 secrets 를 안 넘긴다"
    assert "declared_secrets_for_product(session, run.product_id)" in src


class _Account:
    extra_params = {"executor_type": "claude_code", "worker_id": None}


async def test_an_explicit_env_still_reaches_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """양성 대조군 — **변경 전에도 green.** 검증 스택이 boot 명령에 붙이는 env 는
    이미 워커까지 간다. 이 PR 이 옮기는 것은 그 길이 아니라, 박스가 스스로 들고 다니는
    기본값의 유무다. 이게 깨지면 스택 부팅이 크리덴셜 없이 뜬다."""
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxSession,
    )

    sent = _patch_dispatch(monkeypatch)
    box = ClientWorkerSandboxSession(
        redis=object(),
        session_factory=_Factory(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        workspace_path="/home/founder/proj",
        default_timeout_s=30.0,
        pinned_worker_id=uuid.uuid4(),
    )

    await box.exec("docker compose up", timeout_s=30.0, shell=True, env={"TOKEN": "t"})

    assert sent["env"] == {"TOKEN": "t"}


def _session(*, secrets: dict[str, str] | None) -> Any:
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxSession,
    )

    return ClientWorkerSandboxSession(
        redis=object(),
        session_factory=_Factory(),
        workspace_id=uuid.uuid4(),
        executor_type="claude_code",
        workspace_path="/home/founder/proj",
        default_timeout_s=30.0,
        pinned_worker_id=uuid.uuid4(),
        env=secrets,
    )


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """디스패치 기질을 통째로 가짜로 세우고, 워커에게 나간 것만 기록한다."""
    from backend.executors import dispatch

    seen: dict[str, Any] = {"env": None, "command": ""}

    async def _find(*_a: Any, **_k: Any) -> Any:
        return _Row(uuid.uuid4())

    async def _create(*_a: Any, **kw: Any) -> Any:
        seen["command"] = kw.get("prompt") or ""
        return _Row(uuid.uuid4())

    async def _dispatch(*_a: Any, **kw: Any) -> Any:
        seen["env"] = kw.get("env")
        return None

    async def _await(*_a: Any, **_k: Any) -> Any:
        return _Done()

    monkeypatch.setattr(dispatch, "find_available_worker", _find)
    monkeypatch.setattr(dispatch, "create_task", _create)
    monkeypatch.setattr(dispatch, "dispatch_task", _dispatch)
    monkeypatch.setattr(dispatch, "await_completion", _await)
    return seen


class _Row:
    def __init__(self, rid: uuid.UUID) -> None:
        self.id = rid


class _Done:
    status = "done"
    output = ""
    error_message = None


class _Factory:
    async def __aenter__(self) -> _Factory:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    def __call__(self) -> _Factory:
        return self

    async def commit(self) -> None:
        return None
