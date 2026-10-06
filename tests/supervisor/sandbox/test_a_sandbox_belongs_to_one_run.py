"""#1107 — a sandbox container belongs to ONE run, not to its product.

The container was keyed by product (``bsvibe-sbx-<product id>``) and mounted
the worktree of whichever run created it (``-v <worktree>:/work``). Two
``server_sandbox`` runs of one product at once therefore shared ONE container:
the second run's ``shell_exec`` and verification ran in the FIRST run's
worktree, and whichever run finished first tore the container down under the
other.

Two processes drive the same DinD under the same name — the worker
(``RunOrchestrator``: acquire at run start, release at run end) and the API
(the MCP work tools). Their caches are separate, so the API's first tool call
found nothing in ITS cache and ``docker rm -f``-ed the worker's container to
recreate it. Keying by run alone does not fix that, so a running container of
the right name AND the right worktree is ADOPTED, not recreated; and a slot
held for a container another process already removed is freed instead of
blocking the next run for the 30-minute idle reap.

형님 ruled (2026-10-06): one container per run, adopt, prune.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.mcp.tools import work_registry
from backend.workflow.application.agent_loop import LoopTurn, RunOrchestrator
from backend.workflow.infrastructure.sandbox import (
    DockerSandboxManager,
    NoopSandboxManager,
    reset_sandbox_manager,
)
from tests._support import memory_session
from tests.execution.test_run_orchestrator import ScriptedLlm, _make_run

pytestmark = pytest.mark.asyncio


class _Daemon:
    """One DinD daemon shared by every manager (process) in a test.

    ``boxes`` maps container name → (running, worktree mounted at /work).
    """

    def __init__(self) -> None:
        self.boxes: dict[str, tuple[bool, str]] = {}
        self.created: list[str] = []
        self.removed: list[str] = []

    async def __call__(
        self, argv: list[str], *, timeout_s: float, stdin: bytes | None = None
    ) -> tuple[int | None, bytes, bytes]:
        sub = argv[0]
        if sub == "version":
            return 0, b"24.0.0\n", b""
        if sub == "inspect":
            name = argv[-1]
            if name not in self.boxes:
                return 1, b"", b"No such object"
            running, mount = self.boxes[name]
            state = b"true" if running else b"false"
            if "Mounts" in argv[2]:
                return 0, state + b"|" + mount.encode() + b"\n", b""
            return 0, state + b"\n", b""
        if sub == "rm":
            name = argv[-1]
            if self.boxes.pop(name, None) is not None:
                self.removed.append(name)
            return 0, b"", b""
        if sub == "run":
            name = argv[argv.index("--name") + 1]
            volume = argv[argv.index("-v") + 1]
            self.boxes[name] = (True, volume.rsplit(":", 1)[0])
            self.created.append(name)
            return 0, b"cid\n", b""
        return 0, b"", b""


def _manager(daemon: _Daemon, *, max_concurrent: int = 2) -> DockerSandboxManager:
    mgr = DockerSandboxManager(
        docker_host="tcp://dind:2375",
        sandbox_image="bsvibe-sandbox:test",
        idle_reap_seconds=10,
        max_concurrent=max_concurrent,
    )
    mgr._docker = daemon  # type: ignore[method-assign]
    mgr._swept = True
    return mgr


def _box(name_key: uuid.UUID) -> str:
    return f"bsvibe-sbx-{name_key}"


# ---------------------------------------------------------------------------
# Callers key the box by RUN
# ---------------------------------------------------------------------------


class _Run:
    def __init__(self, product_id: uuid.UUID) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = product_id


class _Ctx:
    session: Any = None
    session_factory: Any = None
    extras: dict[str, Any] = {}  # noqa: RUF012 — read-only stub


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> _Daemon:
    from backend.config import get_settings

    d = _Daemon()

    async def _docker(self: DockerSandboxManager, argv: list[str], **kw: Any) -> Any:
        return await d(argv, **kw)

    async def _no_wait(self: DockerSandboxManager) -> None:
        return None

    monkeypatch.setattr(DockerSandboxManager, "_docker", _docker)
    monkeypatch.setattr(DockerSandboxManager, "_await_dind", _no_wait)
    monkeypatch.setenv("BSVIBE_SANDBOX_ENABLED", "true")
    monkeypatch.setenv("BSVIBE_SANDBOX_IMAGE", "bsvibe-sandbox:test")
    monkeypatch.setenv("BSVIBE_DOCKER_HOST", "tcp://sb:2375")
    get_settings.cache_clear()
    reset_sandbox_manager()
    yield d
    get_settings.cache_clear()
    reset_sandbox_manager()


async def test_two_runs_of_one_product_each_get_their_own_box_on_their_own_worktree(
    tmp_path: Path, daemon: _Daemon
) -> None:
    product = uuid.uuid4()
    first, second = _Run(product), _Run(product)
    first_dir, second_dir = tmp_path / "a", tmp_path / "b"

    await work_registry._sandbox_for(first, first_dir, _Ctx())
    await work_registry._sandbox_for(second, second_dir, _Ctx())

    assert daemon.boxes[_box(first.id)] == (True, str(first_dir))
    assert daemon.boxes[_box(second.id)] == (True, str(second_dir))


class _RecordingManager(NoopSandboxManager):
    def __init__(self) -> None:
        self.acquired: list[uuid.UUID] = []
        self.released: list[uuid.UUID] = []

    async def acquire(self, project_id: uuid.UUID, workspace_path: str):  # noqa: ANN201
        self.acquired.append(project_id)
        return await super().acquire(project_id, workspace_path)

    async def release(self, project_id: uuid.UUID) -> None:
        self.released.append(project_id)


async def test_the_run_loop_holds_and_releases_the_box_of_its_run(tmp_path: Path) -> None:
    manager = _RecordingManager()
    async with memory_session() as session:
        run = await _make_run(session, product_id=uuid.uuid4())
        orch = RunOrchestrator(
            session=session,
            llm=ScriptedLlm([LoopTurn(content="done", tool_calls=())]),
            sandbox_manager=manager,
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    assert manager.acquired == [run.id]
    assert manager.released == [run.id]


# ---------------------------------------------------------------------------
# Two processes, one daemon — adopt, never recreate a live box
# ---------------------------------------------------------------------------


async def test_a_live_box_another_process_started_for_this_run_is_adopted() -> None:
    daemon = _Daemon()
    run_id = uuid.uuid4()
    worker, api = _manager(daemon), _manager(daemon)

    await worker.acquire(run_id, "/app/var/runs/r1")
    await api.acquire(run_id, "/app/var/runs/r1")

    assert daemon.created == [_box(run_id)]
    assert daemon.removed == []


async def test_a_live_box_on_another_worktree_is_not_adopted() -> None:
    daemon = _Daemon()
    run_id = uuid.uuid4()
    daemon.boxes[_box(run_id)] = (True, "/app/var/runs/stale")

    await _manager(daemon).acquire(run_id, "/app/var/runs/r1")

    assert daemon.boxes[_box(run_id)] == (True, "/app/var/runs/r1")


async def test_a_cached_box_is_not_reused_for_another_worktree() -> None:
    daemon = _Daemon()
    key = uuid.uuid4()
    mgr = _manager(daemon)

    await mgr.acquire(key, "/app/var/runs/r1")
    await mgr.acquire(key, "/app/var/runs/r2")

    assert daemon.boxes[_box(key)] == (True, "/app/var/runs/r2")


async def test_a_slot_held_for_a_box_removed_elsewhere_is_freed() -> None:
    """The worker released (``rm -f``) the box the API created — the API's slot must not
    stay held until the 30-minute idle reap, blocking the next run."""
    daemon = _Daemon()
    api = _manager(daemon, max_concurrent=1)
    first, second = uuid.uuid4(), uuid.uuid4()

    await api.acquire(first, "/app/var/runs/r1")
    await daemon(["rm", "-f", _box(first)], timeout_s=1)

    await asyncio.wait_for(api.acquire(second, "/app/var/runs/r2"), timeout=2)

    assert daemon.boxes[_box(second)] == (True, "/app/var/runs/r2")


async def test_idle_reap_forgets_an_adopted_box_without_removing_it() -> None:
    """The process that only adopted a box does not own its lifetime — the run does."""
    daemon = _Daemon()
    run_id = uuid.uuid4()
    worker, api = _manager(daemon), _manager(daemon)
    await worker.acquire(run_id, "/app/var/runs/r1")
    await api.acquire(run_id, "/app/var/runs/r1")

    for entry in api._containers.values():
        entry.last_used = time.monotonic() - 3600
    await api.reap_idle()

    assert _box(run_id) in daemon.boxes
    assert api._containers == {}


async def test_the_run_ending_removes_its_box_even_when_adopted() -> None:
    """``release`` is the run's end — the box goes, whoever created it."""
    daemon = _Daemon()
    run_id = uuid.uuid4()
    api, worker = _manager(daemon), _manager(daemon)
    await api.acquire(run_id, "/app/var/runs/r1")
    await worker.acquire(run_id, "/app/var/runs/r1")

    await worker.release(run_id)

    assert _box(run_id) not in daemon.boxes
