"""A sandbox name collision is retried once, not turned into a failed run.

Measured 2026-11-10 (run ``f33f959b``): a turn that outlived its job made the API create the
run's box between the worker's ``rm -f`` and ``docker run`` -- ``container name ... already in
use`` -- and the run went straight to ``failed (system_error)``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.infrastructure.sandbox import DockerSandboxManager
from backend.workflow.infrastructure.sandbox.errors import SandboxError

pytestmark = pytest.mark.asyncio

_IN_USE = b'docker: Error response from daemon: Conflict. The container name "/bsvibe-sbx-x" is already in use'


class _Daemon:
    """Every ``docker run`` fails with a name conflict ``conflicts`` times, then succeeds."""

    def __init__(self, conflicts: int) -> None:
        self.conflicts = conflicts
        self.runs = 0
        self.removes = 0

    async def __call__(
        self, argv: list[str], *, timeout_s: float, stdin: bytes | None = None, **_kw: Any
    ) -> tuple[int | None, bytes, bytes]:
        if argv[0] == "rm":
            self.removes += 1
        if argv[0] == "run":
            self.runs += 1
            if self.runs <= self.conflicts:
                return 125, b"", _IN_USE
            return 0, b"cid\n", b""
        if argv[0] == "inspect":
            return 1, b"", b"No such object"
        return 0, b"", b""


def _manager(daemon: _Daemon) -> DockerSandboxManager:
    mgr = DockerSandboxManager(
        docker_host="tcp://dind:2375",
        sandbox_image="bsvibe-sandbox:test",
        idle_reap_seconds=10,
        max_concurrent=2,
    )
    mgr._docker = daemon  # type: ignore[method-assign]
    mgr._swept = True
    return mgr


async def test_one_name_conflict_is_cleared_and_retried() -> None:
    daemon = _Daemon(conflicts=1)

    box = await _manager(daemon).acquire(uuid.uuid4(), "/runs/a")

    assert box is not None
    assert daemon.runs == 2
    # the pre-create ``rm -f`` plus the one that clears the conflicting container
    assert daemon.removes == 2


async def test_a_second_conflict_fails_the_create() -> None:
    daemon = _Daemon(conflicts=2)
    mgr = _manager(daemon)

    with pytest.raises(SandboxError, match="already in use"):
        await mgr.acquire(uuid.uuid4(), "/runs/a")

    assert daemon.runs == 2  # exactly one retry, never a loop
    # the slot was given back: the next create is not starved by the failed one
    daemon.conflicts = 0
    await mgr.acquire(uuid.uuid4(), "/runs/b")
