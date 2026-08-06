"""#692 in-place verify B/2b — wiring the client-worker sandbox onto a run.

A ``client_attach`` product's source lives ONLY on the founder's machine, so the
server-side DinD box has nothing to verify. The commands must run where the
source is. This lift picks the sandbox backend PER RUN: a client_attach run gets
a :class:`ClientWorkerSandboxManager` (commands dispatched to the founder's
worker as ``exec`` tasks); every other run keeps today's manager untouched.

Two properties:

1. **Per-run selection, never a silent swap.** client_attach + a usable dispatch
   context → the client-worker manager. Missing any prerequisite (no redis, no
   executor account, no local dir) → the DEFAULT manager, i.e. exactly today's
   behaviour. The gate must never be *quietly* pointed at the wrong machine.

2. **No forced provisioning of the founder's tree.** ``ensure_sandbox_ready``
   runs ``uv sync`` to materialise a venv for a server worktree. On the founder's
   own working directory that is an unasked-for mutation, and it is unnecessary —
   their toolchain is already set up (they work there). A session that declares
   it does not provision is skipped, with NO command dispatched. Because verify's
   ``_ensure_project_venv`` calls the same function, this covers the verify path
   too: gate commands then run bare in the founder's own environment.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.asyncio


class _Box:
    """A SandboxSession stand-in that records every exec it is asked to run."""

    def __init__(self, *, provisions_venv: bool = True) -> None:
        self.provisions_venv = provisions_venv
        self.execs: list[str] = []
        self.reads: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/work"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> Any:
        from backend.workflow.infrastructure.sandbox import SandboxResult

        self.execs.append(command)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        self.reads.append(rel_path)
        return b"version = 1\n"

    async def write_file(self, rel_path: str, content: bytes) -> None:
        raise AssertionError("verify must not write")

    async def list_dir(self, rel_path: str) -> list[str]:
        return []


# ── 1. no forced provisioning of the founder's own tree ──────────────────────


async def test_ensure_sandbox_ready_skips_a_session_that_does_not_provision() -> None:
    """A client-attach box declares ``provisions_venv=False``: NO ``uv sync`` is
    dispatched to the founder's machine, and readiness is honestly False (so the
    verify path does not prepend a ``.venv/bin`` PATH that does not exist)."""
    from backend.workflow.application.sandbox_provisioning import ensure_sandbox_ready

    box = _Box(provisions_venv=False)
    assert await ensure_sandbox_ready(box) is False
    assert box.execs == [], f"nothing may be run on the founder's machine: {box.execs}"
    assert box.reads == [], f"not even a lockfile probe: {box.reads}"


async def test_ensure_sandbox_ready_unchanged_for_a_normal_sandbox() -> None:
    """The server-sandbox path is untouched — a box with a lockfile still syncs."""
    from backend.workflow.application.sandbox_provisioning import ensure_sandbox_ready

    box = _Box()  # provisions_venv defaults True (every existing session)
    assert await ensure_sandbox_ready(box) is True
    assert any("uv sync" in c for c in box.execs), box.execs


# ── 2. per-run sandbox backend selection ─────────────────────────────────────


class _Account:
    def __init__(self, extra: dict[str, Any] | None) -> None:
        self.extra_params = extra


def _default_manager() -> Any:
    from backend.workflow.infrastructure.sandbox import NoopSandboxManager

    return NoopSandboxManager()


def _select(**kwargs: Any) -> Any:
    from backend.workflow.application.runtime.sandbox_selection import sandbox_manager_for_run

    return sandbox_manager_for_run(**kwargs)


def _base_kwargs(**over: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "default": _default_manager(),
        "execution_target": "client_attach",
        "client_workspace_dir": "/Users/founder/proj",
        "account": _Account({"executor_type": "claude_code", "worker_id": str(uuid.uuid4())}),
        "redis_client": object(),
        "session_factory": object(),
        "workspace_id": uuid.uuid4(),
        "timeout_s": 900.0,
    }
    kwargs.update(over)
    return kwargs


async def test_client_attach_run_gets_the_client_worker_manager() -> None:
    """The gate must run WHERE THE SOURCE IS — the founder's machine."""
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (
        ClientWorkerSandboxManager,
    )

    assert isinstance(_select(**_base_kwargs()), ClientWorkerSandboxManager)


async def test_server_sandbox_run_keeps_the_default_manager() -> None:
    """Every non-client_attach run is untouched by this lift."""
    default = _default_manager()
    got = _select(**_base_kwargs(default=default, execution_target="server_sandbox"))
    assert got is default


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("redis_client", None),  # nothing to dispatch onto the worker stream
        ("session_factory", None),  # no connection-free session to await on
        ("client_workspace_dir", None),  # no directory on the founder's machine
        ("account", _Account({})),  # not an executor account → no worker
        ("account", _Account(None)),
    ],
)
async def test_missing_prerequisite_falls_back_to_the_default_manager(
    field: str, value: Any
) -> None:
    """A client_attach run whose dispatch context is incomplete keeps the DEFAULT
    manager — today's behaviour (the run settles UNTESTED). What must never
    happen is a gate quietly running against the WRONG machine."""
    default = _default_manager()
    got = _select(**_base_kwargs(default=default, **{field: value}))
    assert got is default


async def test_client_manager_is_pinned_to_the_founders_worker() -> None:
    """The gate is pinned to the SAME worker the agent turns ran on — that is the
    machine holding this product's working tree. Another workspace worker would
    not have the source at all."""
    worker_id = uuid.uuid4()
    mgr = _select(
        **_base_kwargs(
            account=_Account({"executor_type": "claude_code", "worker_id": str(worker_id)})
        )
    )
    box = await mgr.acquire(uuid.uuid4(), "/Users/founder/proj")
    assert box.workspace_mount == "/Users/founder/proj"
    assert mgr._pinned_worker_id == worker_id
    # The client-attach box must NOT provision the founder's tree.
    assert getattr(box, "provisions_venv", True) is False
