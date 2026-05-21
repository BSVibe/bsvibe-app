"""Sandbox script runner — per-project disposable execution containers.

Lifted from BSNexus ``backend/src/core/sandbox/``. The work-loop /
verifier tool path runs commands inside a per-project sandbox container
managed in a DinD sidecar; ``NoopSandboxManager`` is the host-side
fallback used while ``sandbox_enabled`` is false.

Public surface::

    from backend.supervisor.sandbox import (
        SandboxManager, SandboxSession, SandboxResult,
        SandboxError, SandboxUnavailable,
        DockerSandboxManager, NoopSandboxManager,
        get_sandbox_manager, build_sandbox_manager, sandbox_reaper_loop,
    )

The image name defaults to ``bsvibe-sandbox:latest``; the container
name prefix is ``bsvibe-sbx-<project_id>``. ``deploy/Dockerfile.sandbox``
+ ``tools/build-sandbox-image.sh`` build the image (deferred to the
ops bundle that wires DinD into the compose stack).
"""

from __future__ import annotations

from backend.supervisor.sandbox.docker_manager import (
    DockerSandboxManager,
    DockerSandboxSession,
)
from backend.supervisor.sandbox.errors import SandboxError, SandboxUnavailable
from backend.supervisor.sandbox.noop_manager import (
    NoopSandboxManager,
    NoopSandboxSession,
)
from backend.supervisor.sandbox.protocol import (
    SandboxManager,
    SandboxResult,
    SandboxSession,
)
from backend.supervisor.sandbox.reaper import REAP_INTERVAL_S, sandbox_reaper_loop
from backend.supervisor.sandbox.resolver import (
    build_sandbox_manager,
    get_sandbox_manager,
    reset_sandbox_manager,
)

__all__ = [
    "REAP_INTERVAL_S",
    "DockerSandboxManager",
    "DockerSandboxSession",
    "NoopSandboxManager",
    "NoopSandboxSession",
    "SandboxError",
    "SandboxManager",
    "SandboxResult",
    "SandboxSession",
    "SandboxUnavailable",
    "build_sandbox_manager",
    "get_sandbox_manager",
    "reset_sandbox_manager",
    "sandbox_reaper_loop",
]
