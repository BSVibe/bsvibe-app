"""The MCP work-tool transport must REUSE the run's sandbox across tool calls.

Regression guard for the E1 defect (proven live in prod, 2026-07-14): ``_sandbox_for``
resolved the sandbox via ``build_sandbox_manager()`` — the UNCACHED builder — on **every**
MCP tool call. Each call got a fresh :class:`DockerSandboxManager` with an empty
``_containers`` cache, so ``acquire`` never hit the reuse branch and ``_create`` fired every
time: ``docker rm -f`` + ``docker run`` per ``file_read``. That is the 300s-timeout tax, and
it tears down the very container a parallel native run is verifying in (same ``project_id``).

The fix routes ``_sandbox_for`` through the process singleton ``get_sandbox_manager()`` so all
tool calls in the API process share one manager → one cache → the container is created once
and reused. This test observes the REAL effect (container-create count), not a proxy — the
class of bug that unit/CI green never caught, because only actual reuse across calls reveals it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from backend.mcp.tools import work_registry
from backend.workflow.infrastructure.sandbox import (
    DockerSandboxManager,
    reset_sandbox_manager,
)

pytestmark = pytest.mark.asyncio


class _Run:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()  # same product across the "tool calls"


@pytest.fixture
def _fake_docker(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Stub the docker CLI at the class level and count container creations.

    ``run`` = a real container create. ``inspect`` reports a name as running once it has been
    created, so the manager's reuse branch can fire. No real docker is touched.
    """
    counts = {"run": 0}
    created: set[str] = set()

    async def _fake_await_dind(self: DockerSandboxManager) -> None:
        return None

    async def _fake_docker(
        self: DockerSandboxManager, args: list[str], *, timeout_s: float = 0.0
    ) -> tuple[int, bytes, bytes]:
        cmd = args[0]
        if cmd == "run":
            name = args[args.index("--name") + 1]
            created.add(name)
            counts["run"] += 1
            return 0, b"", b""
        if cmd == "inspect":
            name = args[-1]
            return (0, b"true", b"") if name in created else (1, b"", b"")
        if cmd == "rm":
            return 0, b"", b""
        return 0, b"", b""

    monkeypatch.setattr(DockerSandboxManager, "_await_dind", _fake_await_dind)
    monkeypatch.setattr(DockerSandboxManager, "_docker", _fake_docker)
    return counts


@pytest.fixture(autouse=True)
def _sandbox_enabled(monkeypatch: pytest.MonkeyPatch):
    from backend.config import get_settings

    monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "true")
    monkeypatch.setenv("BSVIBE_SANDBOX_IMAGE", "bsvibe-sandbox:test")
    monkeypatch.setenv("BSVIBE_DOCKER_HOST", "tcp://sb:2375")
    get_settings.cache_clear()
    reset_sandbox_manager()
    yield
    get_settings.cache_clear()
    reset_sandbox_manager()


async def test_sandbox_created_once_across_many_tool_calls(
    tmp_path: Path, _fake_docker: dict[str, int]
) -> None:
    """Three MCP tool calls on the same run create the container ONCE, then reuse it."""
    run = _Run()
    wsdir = str(tmp_path)

    for _ in range(3):
        session = await work_registry._sandbox_for(run, Path(wsdir))
        assert session is not None

    assert _fake_docker["run"] == 1, (
        f"expected the container to be created once and reused, but it was created "
        f"{_fake_docker['run']} times — the sandbox is being torn down and rebuilt per tool call"
    )


async def test_sandbox_for_uses_the_process_singleton(
    tmp_path: Path, _fake_docker: dict[str, int]
) -> None:
    """Every call resolves the SAME manager instance (so its container cache persists)."""
    run = _Run()
    s1 = await work_registry._sandbox_for(run, tmp_path)
    s2 = await work_registry._sandbox_for(run, tmp_path)
    # DockerSandboxSession carries a back-reference to its manager; both must be the one singleton.
    assert s1._mgr is s2._mgr
