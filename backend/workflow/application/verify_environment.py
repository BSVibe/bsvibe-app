"""WHERE a verification check runs — the seam between a stack and the commands.

#726/#728 derived how a product's disposable instance boots and #730 gave every
product one; nothing ran inside it. This module is the wiring, and it exists as
its own seam for one reason: a stack stood up NEXT TO checks that keep running
on the founder's machine isolates nothing while claiming to. The environment has
to be the thing commands go through, not a thing that happens alongside them.

What the caller gets is a box or an explanation:

``kind="container"`` / ``"compose"`` / ``"metadata"``
    An environment is up. For a container the box is REWRITTEN: commands go
    through ``docker exec``, ``workspace_mount`` moves to the container's
    workdir, and dependency provisioning is turned back ON (a client_attach box
    declines it because ``uv sync`` in the founder's own tree is an unasked-for
    mutation — a disposable container is the opposite case).
``kind="host"``
    The product DECLARED that its checks run on the host, or its repo declares
    no toolchain to reproduce. The founder's own box, unchanged.
``kind="unavailable"``
    No slot free, or the boot failed. **No box at all.** Falling back to the
    host here would answer a different question than the report will claim was
    answered, and nobody reading it could tell — the exact class of quietly
    wrong result this track exists to remove.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.application.verification_stack import (
    StackNotApplicable,
    StackReady,
    StackUnavailable,
    open_verification_stack,
)
from backend.workflow.infrastructure.sandbox import SandboxError, SandboxResult, SandboxSession

logger = structlog.get_logger(__name__)

#: Listing the repo is a directory read on the founder's machine, not work.
_LIST_TIMEOUT_S = 30.0

#: Where the derivation looks for the compose pair. Listed explicitly because
#: ``list_dir`` is shallow and the safety rule (#724: a compose stack without
#: the isolation overlay is REFUSED) can only be applied if both names are seen.
_COMPOSE_DIR = "deploy"


@dataclass(frozen=True)
class CheckEnvironment:
    """Where this run's checks run — or why they cannot run anywhere."""

    box: SandboxSession | None
    #: ``container`` / ``compose`` / ``metadata`` / ``host`` / ``unavailable``.
    kind: str
    #: Set only for ``unavailable``: the infrastructure fact, never a verdict.
    unavailable: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """The evidence-trail record of this environment.

        "Which environment did this check run in" is half of what the check
        means (design §3.1: the environment is a FIELD of the check, not a new
        kind of proof state)."""
        return {"kind": self.kind, **self.detail}


class _StackBox:
    """A :class:`SandboxSession` whose COMMANDS run inside the environment.

    Reads keep going to the founder's tree: manifests and git answers are about
    the source under test, and only the commands need isolating. (The container
    holds a copy of that same tree, so the two agree.)
    """

    #: Still the founder's machine — the drive loop's in-place decisions hold.
    runs_in_place = True
    #: Nothing is installed in a fresh container yet, and reproducing the
    #: declared dependencies there is the entire point of standing it up.
    provisions_venv = True

    def __init__(self, inner: SandboxSession, ready: StackReady) -> None:
        self._inner = inner
        self._ready = ready

    @property
    def workspace_mount(self) -> str:
        """The source's path INSIDE the environment.

        Callers build absolute paths from this (the venv ``PATH`` prefix). A
        founder-machine path inside a container resolves to nothing — silently,
        so every command would run without its toolchain and fail for a reason
        that has nothing to do with the change.
        """
        return self._ready.plan.workdir or self._inner.workspace_mount

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        # ``shell=True`` regardless of the caller's flag: the wrapped form is a
        # shell string by construction. (S604: this is the sandbox Protocol's
        # own flag, not a subprocess call — the command is the repo's declared
        # check, which is the whole point of the gate.)
        return await self._inner.exec(  # noqa: S604
            self._ready.wrap(command), timeout_s=timeout_s, shell=True
        )

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return await self._inner.read_file(rel_path, max_bytes)

    async def list_dir(self, rel_path: str) -> list[str]:
        return await self._inner.list_dir(rel_path)

    async def write_file(self, rel_path: str, content: bytes) -> None:
        # Fail-closed rather than write somewhere the checks cannot see: the
        # environment holds a COPY taken at boot, so a write to the founder's
        # tree now would be invisible to every command that follows.
        raise SandboxError(
            "writing into the disposable verification environment is not wired: "
            "a write to the founder's tree would be invisible to the checks"
        )


@asynccontextmanager
async def open_check_environment(
    *,
    box: SandboxSession,
    slot_project: str | None,
    repo_files: Sequence[str],
    metadata: Mapping[str, Any] | None,
    docker_context: str,
    boot_timeout_s: float,
) -> AsyncIterator[CheckEnvironment]:
    """Hold the environment this run's checks run in, for the duration.

    ``slot_project`` is the compose project / container name of a slot the
    caller ALREADY HOLDS; ``None`` means the concurrency budget was exhausted,
    which is an infrastructure fact and not a licence to run somewhere else.
    """
    if slot_project is None:
        reason = "no verification slot free (concurrent-verification budget exhausted)"
        logger.info("check_environment_unavailable", reason=reason)
        yield CheckEnvironment(box=None, kind="unavailable", unavailable=reason)
        return

    async with open_verification_stack(
        box=box,
        slot_project=slot_project,
        repo_files=repo_files,
        metadata=metadata,
        docker_context=docker_context,
        boot_timeout_s=boot_timeout_s,
    ) as outcome:
        if isinstance(outcome, StackUnavailable):
            yield CheckEnvironment(box=None, kind="unavailable", unavailable=outcome.reason)
            return
        if isinstance(outcome, StackNotApplicable):
            yield CheckEnvironment(box=box, kind="host")
            return
        assert isinstance(outcome, StackReady)  # noqa: S101 — exhaustive over StackOutcome
        plan = outcome.plan
        detail = {"source": plan.source, "image": plan.image, "project": outcome.project}
        if not plan.exec_template:
            # An environment is up but offers no way in. Since the compose
            # prober landed this is a narrow case — a compose repo declaring no
            # toolchain at all, where inventing an image would be a fabrication.
            # The checks then run where they already ran, and the stack's
            # services are simply not reachable from there.
            logger.info("verify_stack_has_no_way_in", project=outcome.project, source=plan.source)
            yield CheckEnvironment(box=box, kind=plan.source, detail=detail)
            return
        yield CheckEnvironment(box=_StackBox(box, outcome), kind=plan.source, detail=detail)


async def list_repo_files(box: SandboxSession) -> list[str]:
    """The repo-relative names the stack derivation reasons about.

    Top level plus ``deploy/``: shallow everywhere else, because what the
    derivation asks is "does this repo declare a toolchain / a compose pair",
    and both questions live at those two levels. An unreadable directory
    contributes nothing rather than a guess — and contributing nothing is the
    SAFE direction here, since the compose branch is the one that can collide
    with production.
    """
    names: list[str] = []
    for prefix in ("", _COMPOSE_DIR):
        try:
            entries = await box.list_dir(prefix or ".")
        except Exception as exc:  # noqa: BLE001 — an unreadable dir is not a claim
            logger.info("repo_listing_failed", path=prefix or ".", error=str(exc))
            continue
        for entry in entries:
            name = entry.rstrip("/")
            if not name:
                continue
            names.append(f"{prefix}/{name}" if prefix else name)
    return names


async def _product_metadata(session: AsyncSession, product_id: uuid.UUID) -> Mapping[str, Any]:
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.workspaces_db import ProductRow  # noqa: PLC0415

    try:
        raw = await session.scalar(
            select(ProductRow.product_metadata).where(ProductRow.id == product_id)
        )
    except Exception:  # noqa: BLE001 — an unreadable product must not break the run
        logger.warning("verify_metadata_lookup_failed", product_id=str(product_id), exc_info=True)
        return {}
    return raw if isinstance(raw, Mapping) else {}


@asynccontextmanager
async def open_run_check_environment(
    *, session: AsyncSession, run: Any, box: SandboxSession
) -> AsyncIterator[CheckEnvironment]:
    """The run-level entry point: read the product's declaration, take a slot,
    stand the environment up.

    The slot is held on a **dedicated connection** (see
    :func:`~backend.workflow.infrastructure.verify_slots.open_slot_session`).
    That is not incidental: the lock is session-scoped, so it must live on a
    connection that dies with this run — and it must not be the run's own
    session, which commits (returning its connection to the pool) all through
    verification precisely so nothing is held across the long steps.
    """
    from backend.config import get_settings  # noqa: PLC0415
    from backend.workflow.infrastructure.verify_slots import (  # noqa: PLC0415
        acquire_verify_slot,
        load_workspace_verify_slots,
        open_slot_session,
    )

    settings = get_settings()
    metadata = await _product_metadata(session, run.product_id) if run.product_id else {}
    slots = await load_workspace_verify_slots(session, run.workspace_id)
    repo_files = await list_repo_files(box)

    async with (
        open_slot_session() as slot_session,
        acquire_verify_slot(slot_session, slots=slots) as slot,
    ):
        async with open_check_environment(
            box=box,
            slot_project=slot.project if slot is not None else None,
            repo_files=repo_files,
            metadata=metadata,
            docker_context=settings.verify_docker_context,
            boot_timeout_s=settings.verify_stack_boot_timeout_s,
        ) as environment:
            yield environment


__all__ = [
    "CheckEnvironment",
    "list_repo_files",
    "open_check_environment",
    "open_run_check_environment",
]
