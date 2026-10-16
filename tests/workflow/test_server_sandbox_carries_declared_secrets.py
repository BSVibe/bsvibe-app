"""#691 — a ``server_sandbox`` product's commands get the secrets it declared.

``products.metadata.verify_secrets`` already held them (sealed), and a ``client_attach`` run
already carried them — its box holds them and every command it dispatches gets them
(``test_agent_exec_carries_declared_secrets.py``). The server sandbox did not:

* ``DockerSandboxSession.exec`` accepted ``env`` and DROPPED it — ``docker exec`` was built
  without it, so even the verification stack's own boot env never arrived;
* ``sandbox_manager_for_run`` handed a non-client_attach run the process-wide box untouched,
  so nothing the product declared reached it;
* the MCP work tools acquired the same box with no secrets either.

So a product that needs its own API key could be written by BSVibe but not RUN by it on the
server. Here the box carries them, the same shape as the client box.

Values travel only as process environment: ``docker exec -e NAME`` takes the value from the
docker CLI's own environment, so no secret is ever on an argv (``ps``, error text, logs).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from backend.workflow.infrastructure.sandbox import (
    DockerSandboxManager,
    NoopSandboxManager,
    SandboxResult,
)


class _Recorder:
    """The ``_docker`` boundary — records argv and the extra env it was handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    async def __call__(
        self,
        argv: list[str],
        *,
        timeout_s: float,
        stdin: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int | None, bytes, bytes]:
        self.calls.append((argv, extra_env))
        if argv[0] == "inspect":
            return 0, b"true\n", b""
        return 0, b"", b""


def _docker_mgr() -> tuple[DockerSandboxManager, _Recorder]:
    mgr = DockerSandboxManager(
        docker_host="tcp://dind:2375",
        sandbox_image="bsvibe-sandbox:test",
        idle_reap_seconds=10,
        max_concurrent=2,
    )
    rec = _Recorder()
    mgr._docker = rec  # type: ignore[method-assign]
    return mgr, rec


def _execs(rec: _Recorder) -> list[tuple[list[str], dict[str, str] | None]]:
    return [c for c in rec.calls if c[0][0] == "exec"]


# --- the docker session honours env ---------------------------------------------------------


async def test_docker_exec_passes_env_by_name_only(tmp_path: Path) -> None:
    mgr, rec = _docker_mgr()
    box = await mgr.acquire(uuid.uuid4(), str(tmp_path))

    await box.exec("python report.py", timeout_s=5.0, env={"ALPACA_API_KEY": "s3cret"})

    argv, extra_env = _execs(rec)[-1]
    i = argv.index("-e")
    assert argv[i + 1] == "ALPACA_API_KEY", argv
    assert "s3cret" not in " ".join(argv), "a secret value reached the docker argv"
    assert extra_env == {"ALPACA_API_KEY": "s3cret"}
    assert argv[-2:] == ["python", "report.py"]


async def test_docker_exec_without_env_is_unchanged(tmp_path: Path) -> None:
    """Control — no env, the argv is exactly what it always was."""
    mgr, rec = _docker_mgr()
    box = await mgr.acquire(uuid.uuid4(), str(tmp_path))

    await box.exec("pytest -q", timeout_s=5.0)

    argv, extra_env = _execs(rec)[-1]
    assert "-e" not in argv
    assert not extra_env


async def test_the_docker_cli_receives_the_value_in_its_environment(monkeypatch) -> None:
    """The real boundary: the value goes into the subprocess env, never its argv."""
    import asyncio

    seen: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:  # noqa: A002
            return b"", b""

    async def _spawn(*argv: str, **kwargs: Any) -> _Proc:
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spawn)
    mgr = DockerSandboxManager(
        docker_host="tcp://dind:2375",
        sandbox_image="x",
        idle_reap_seconds=10,
        max_concurrent=1,
    )

    await mgr._docker(["exec", "-e", "K", "c", "true"], timeout_s=5.0, extra_env={"K": "v"})

    assert seen["env"]["K"] == "v"
    assert seen["env"]["DOCKER_HOST"] == "tcp://dind:2375"
    assert "v" not in seen["argv"]


# --- the box carries the product's secrets --------------------------------------------------


async def test_a_box_with_env_gives_it_to_every_command(tmp_path: Path) -> None:
    from backend.workflow.infrastructure.sandbox import with_declared_env

    mgr, rec = _docker_mgr()
    carried = with_declared_env(mgr, {"ALPACA_API_KEY": "k", "KEEP": "y"})
    box = await carried.acquire(uuid.uuid4(), str(tmp_path))

    await box.exec("uv run report", timeout_s=5.0, shell=True)
    await box.exec("boot", timeout_s=5.0, shell=True, env={"ALPACA_API_KEY": "caller"})

    first, second = _execs(rec)[-2:]
    assert first[1] == {"ALPACA_API_KEY": "k", "KEEP": "y"}
    assert second[1] == {"ALPACA_API_KEY": "caller", "KEEP": "y"}, "the caller's env must win"


async def test_no_declared_secrets_keeps_the_box_itself() -> None:
    """Control — a product that declares nothing gets the identical manager object."""
    from backend.workflow.infrastructure.sandbox import with_declared_env

    mgr, _ = _docker_mgr()

    assert with_declared_env(mgr, {}) is mgr
    assert with_declared_env(mgr, None) is mgr


async def test_the_carrying_box_keeps_the_inner_boxs_traits(tmp_path: Path) -> None:
    """``runs_in_place`` / ``provisions_venv`` are read off the session by ``getattr`` — a
    wrapper that hid them would silently change how the loop treats the box."""
    from backend.workflow.infrastructure.sandbox import with_session_env

    class _Inner:
        runs_in_place = True
        provisions_venv = False
        workspace_mount = "/w"

        async def exec(self, command: str, **kw: Any) -> SandboxResult:
            return SandboxResult(0, "", "", False)

    box = with_session_env(_Inner(), {"A": "1"})

    assert box.runs_in_place is True
    assert box.provisions_venv is False
    assert box.workspace_mount == "/w"


async def test_the_host_box_honours_env(tmp_path: Path) -> None:
    """The sandbox-disabled fallback runs on the host; it must not drop env either."""
    box = await NoopSandboxManager().acquire(uuid.uuid4(), str(tmp_path))

    res = await box.exec(
        'printf %s "$BSVIBE_T691"', timeout_s=5.0, shell=True, env={"BSVIBE_T691": "ok"}
    )

    assert res.stdout == "ok"


# --- the wiring: loop path and MCP path -----------------------------------------------------


def test_the_loop_seam_gives_a_server_run_its_secrets() -> None:
    from backend.workflow.application.runtime.sandbox_selection import sandbox_manager_for_run

    mgr, _ = _docker_mgr()

    picked = sandbox_manager_for_run(
        default=mgr,
        execution_target="server_sandbox",
        client_workspace_dir=None,
        account=None,
        redis_client=None,
        session_factory=None,
        workspace_id=uuid.uuid4(),
        timeout_s=30.0,
        secrets={"ALPACA_API_KEY": "k"},
    )

    assert picked is not mgr, "a server run that declared secrets got the bare box"
    assert picked._env == {"ALPACA_API_KEY": "k"}  # type: ignore[attr-defined]


def test_the_loop_seam_leaves_a_server_run_without_secrets_alone() -> None:
    from backend.workflow.application.runtime.sandbox_selection import sandbox_manager_for_run

    mgr, _ = _docker_mgr()

    picked = sandbox_manager_for_run(
        default=mgr,
        execution_target="server_sandbox",
        client_workspace_dir=None,
        account=None,
        redis_client=None,
        session_factory=None,
        workspace_id=uuid.uuid4(),
        timeout_s=30.0,
        secrets={},
    )

    assert picked is mgr


async def test_the_mcp_work_tools_box_carries_the_secrets(tmp_path: Path, monkeypatch) -> None:
    from backend.mcp.tools import work_registry

    mgr, rec = _docker_mgr()
    monkeypatch.setattr(work_registry, "get_sandbox_manager", lambda: mgr)

    async def _secrets(run: Any, ctx: Any) -> dict[str, str]:
        return {"ALPACA_API_KEY": "k"}

    class _Ctx:
        session: Any = None
        session_factory: Any = None
        extras: dict[str, Any] = {"declared_secrets": _secrets}  # noqa: RUF012

    class _Run:
        id = uuid.uuid4()
        workspace_id = uuid.uuid4()
        product_id = uuid.uuid4()

    box = await work_registry._sandbox_for(_Run(), tmp_path, _Ctx())
    await box.exec("python report.py", timeout_s=5.0, shell=True)

    assert _execs(rec)[-1][1] == {"ALPACA_API_KEY": "k"}


async def test_the_injected_resolver_unseals_the_products_secrets(monkeypatch) -> None:
    from backend.workflow.application import mcp_work_effects

    seen: dict[str, Any] = {}

    async def _declared(session: Any, product_id: uuid.UUID) -> dict[str, str]:
        seen["product_id"] = product_id
        return {"K": "v"}

    class _Run:
        product_id = uuid.uuid4()

    class _Ctx:
        session = object()

    import backend.workflow.application.runtime.sandbox_selection as sel

    monkeypatch.setattr(sel, "declared_secrets_for_product", _declared)
    run = _Run()

    assert await mcp_work_effects.resolve_declared_secrets(run, _Ctx()) == {"K": "v"}
    assert seen["product_id"] == run.product_id


def test_the_composition_root_injects_the_secret_resolver() -> None:
    """Absent the injection the MCP box silently runs without the secrets — no error at all."""
    import inspect

    from backend.api import main
    from backend.mcp import lifespan, server

    assert "declared_secrets" in inspect.signature(server.build_server).parameters
    assert "declared_secrets" in inspect.signature(lifespan.mcp_lifespan).parameters
    assert "declared_secrets=resolve_declared_secrets" in inspect.getsource(main)
    # ...and the server hands it to the tool context — the parameter alone proves nothing.
    assert 'extras["declared_secrets"] = declared_secrets' in inspect.getsource(server)
