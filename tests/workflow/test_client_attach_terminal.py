"""#692 — client_attach runs keep their source LOCAL and reach a terminal.

Two properties, both consequences of the founder's client_attach contract
("BSVibe orchestration, but my source never leaves my machine"):

1. **No server-side source.** The W1 workspace provisioner clones the product's
   repo into a server-side run worktree. For a ``client_attach`` product that
   would put the user's source on the server — exactly what choosing local
   execution rejects. The provisioner must no-op.

2. **The run terminates.** The agent works through the CLI's NATIVE tools on the
   user's machine, so the server observes NO ``written_paths``. The drive loop's
   "no work yet" nudge (and then the verify/contract path) therefore never
   settles: live E2E (2026-08-05) left a client_attach run looping for 3 hours,
   re-acting on the user's clone. A client_attach run finishes at
   ``review_ready`` on the worker task's success — the founder reviews the local
   changes in their own workspace, as with Claude Code.

Pure helpers + the composite provisioner, tested directly (the full drive loop
needs a whole RunOrchestrator — mirrors ``test_drive_loop_merge_conflict.py``).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.workflow.application.runtime.workspace_provisioning import (
    _build_composite_workspace_provisioner,
)

pytestmark = pytest.mark.asyncio


class _Run:
    """Minimal ExecutionRun stand-in: the provisioner only needs these."""

    def __init__(self, product_id: uuid.UUID | None) -> None:
        self.id = uuid.uuid4()
        self.product_id = product_id
        self.workspace_id = uuid.uuid4()
        self.payload: dict[str, Any] = {}


async def test_composite_provisioner_skips_server_worktree_for_client_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client_attach run gets NO server-side clone — neither provisioner runs."""
    import backend.workflow.application.runtime.workspace_provisioning as rt

    calls: list[str] = []

    async def _github(session, run, workspace_dir):  # noqa: ANN001, ARG001
        calls.append("github")

    async def _product(session, run, workspace_dir):  # noqa: ANN001, ARG001
        calls.append("product")
        return True

    async def _yes(session, product_id):  # noqa: ANN001, ARG001
        return True

    monkeypatch.setattr(rt, "product_is_client_attach", _yes)
    provision = _build_composite_workspace_provisioner(github=_github, product=_product)
    await provision(object(), _Run(uuid.uuid4()), tmp_path)

    assert calls == [], (
        "client_attach must NOT clone the user's source onto the server "
        f"(provisioners that ran: {calls})"
    )


async def test_composite_provisioner_still_runs_for_server_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (server_sandbox) path is unchanged — github first, then product."""
    import backend.workflow.application.runtime.workspace_provisioning as rt

    calls: list[str] = []

    async def _github(session, run, workspace_dir):  # noqa: ANN001, ARG001
        calls.append("github")

    async def _product(session, run, workspace_dir):  # noqa: ANN001, ARG001
        calls.append("product")
        return True

    async def _no(session, product_id):  # noqa: ANN001, ARG001
        return False

    monkeypatch.setattr(rt, "product_is_client_attach", _no)
    provision = _build_composite_workspace_provisioner(github=_github, product=_product)
    await provision(object(), _Run(uuid.uuid4()), tmp_path)

    assert calls == ["github", "product"]


async def test_composite_provisioner_runs_for_run_without_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with no product cannot be client_attach — unchanged behaviour, and
    no product lookup is attempted."""
    import backend.workflow.application.runtime.workspace_provisioning as rt

    calls: list[str] = []

    async def _github(session, run, workspace_dir):  # noqa: ANN001, ARG001
        calls.append("github")

    async def _product(session, run, workspace_dir):  # noqa: ANN001, ARG001
        calls.append("product")
        return True

    async def _boom(session, product_id):  # noqa: ANN001, ARG001
        raise AssertionError("must not look up a product for a product-less run")

    monkeypatch.setattr(rt, "product_is_client_attach", _boom)
    provision = _build_composite_workspace_provisioner(github=_github, product=_product)
    await provision(object(), _Run(None), tmp_path)

    assert calls == ["github", "product"]
