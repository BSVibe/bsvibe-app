"""#692 in-place verify — per-run sandbox backend selection.

A ``client_attach`` product's source lives ONLY on the founder's machine, so the
server-side sandbox has nothing to verify: its gate commands must run where the
source is. This module holds that one decision, kept out of
:mod:`...runtime.agent_runtime` so the factory stays under its LOC ceiling.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.config import Settings
from backend.workflow.infrastructure.sandbox import (
    NoopSandboxManager,
    SandboxManager,
    get_sandbox_manager,
)

logger = structlog.get_logger(__name__)


def sandbox_manager_for_run(
    *,
    default: SandboxManager,
    execution_target: str,
    client_workspace_dir: str | None,
    account: Any,
    redis_client: Any,
    session_factory: async_sessionmaker[AsyncSession] | None,
    workspace_id: uuid.UUID,
    timeout_s: float,
) -> SandboxManager:
    """#692 in-place verify — pick the sandbox backend for THIS run.

    A ``client_attach`` product's source lives only on the founder's machine, so
    the server-side box has nothing to verify: its commands must run where the
    source is. Such a run gets a
    :class:`~backend.workflow.infrastructure.sandbox.client_worker_manager.ClientWorkerSandboxManager`,
    which dispatches each command to that machine as an ``exec`` task and takes
    the exit code as the verdict — the SAME honesty the server sandbox gate rests
    on ("the command's exit code decides, never a model's opinion").

    The gate is pinned to the worker the agent turns ran on (the account's
    ``worker_id``): that is the machine holding this product's working tree —
    another workspace worker would not have the source at all.

    Every other run keeps ``default`` untouched. So does a client_attach run
    whose dispatch context is incomplete (no redis / no session factory / no
    local dir / not an executor account): that degrades to today's behaviour
    (the run settles UNTESTED), which is honest. What must never happen is a
    gate quietly running against the WRONG machine.
    """
    if execution_target != "client_attach" or not client_workspace_dir:
        return default
    extra = getattr(account, "extra_params", None) or {}
    executor_type = str(extra.get("executor_type") or "")
    if not executor_type or redis_client is None or session_factory is None:
        logger.info(
            "client_attach_gate_context_incomplete",
            workspace_id=str(workspace_id),
            has_executor_type=bool(executor_type),
            has_redis=redis_client is not None,
            has_session_factory=session_factory is not None,
        )
        return default
    from backend.workflow.infrastructure.sandbox.client_worker_manager import (  # noqa: PLC0415
        ClientWorkerSandboxManager,
    )

    pinned = extra.get("worker_id")
    try:
        pinned_worker_id = uuid.UUID(str(pinned)) if pinned else None
    except (TypeError, ValueError):
        pinned_worker_id = None
    return ClientWorkerSandboxManager(
        redis=redis_client,
        session_factory=session_factory,
        workspace_id=workspace_id,
        executor_type=executor_type,
        pinned_worker_id=pinned_worker_id,
        default_timeout_s=timeout_s,
        # The founder's own tree — the ONLY path that exists on that machine. The
        # ``acquire`` caller passes the run's server-side dir, which does not.
        client_workspace_dir=client_workspace_dir,
    )


def resolve_sandbox_manager(
    sandbox_manager: SandboxManager | None, settings: Settings
) -> SandboxManager:
    """Pick the sandbox backend EXPLICITLY — never a silent host fallback.

    [[bsvibe-no-implicit-routing]]: an injected manager (tests) wins; otherwise
    when ``sandbox_enabled`` the Docker (DinD) manager MUST build — an
    enabled-but-unbuildable sandbox raises rather than degrading to host
    execution (the old ``… or NoopSandboxManager()`` tail silently ran the
    verifier's ``command`` checks as worker-container subprocesses, where the
    project toolchain is absent). Only when the sandbox is *explicitly* disabled
    do we use the host :class:`NoopSandboxManager`.
    """
    if sandbox_manager is not None:
        return sandbox_manager
    if settings.sandbox_enabled:
        built = get_sandbox_manager()
        if built is None:
            raise RuntimeError(
                "sandbox_enabled is true but no sandbox manager could be built — "
                "refusing to silently fall back to host execution"
            )
        return built
    return NoopSandboxManager()
