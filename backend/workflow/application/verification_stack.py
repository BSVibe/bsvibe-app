"""Stand one disposable instance of the product up, drive it, guarantee it goes.

This composes the pieces built for full-surface verification:

* the concurrency **slot** (:mod:`...infrastructure.verify_slots`) names the
  compose project, so reclaiming a slot reclaims a dead holder's stack;
* the derived **plan** (:mod:`...domain.verify_stack`) says how this particular
  product boots — or that it has no stack at all;
* the worker's **exec** channel runs the commands on the founder's machine,
  which is OUTSIDE the stack under verification: a broken stack then yields an
  error rather than the silence an in-stack prober would produce.

Three outcomes, fail-CLOSED — the same shape the derived gate already uses,
because the distinctions matter for honesty:

``StackNotApplicable``
    This product has no stack. A CLI's repo IS its environment. Not a failure.
``StackUnavailable``
    No slot free, the plan is unusable, or the boot failed. **Not a
    verification failure** — "could not stand it up" and "stood it up and the
    probe failed" are different facts, and conflating them either invents a
    defect or hides one.
``StackReady``
    Up. Probes may run against it.

Teardown is in ``finally`` so a raising probe cannot leak a stack — but the
design does NOT rely on that: a killed process never reaches ``finally``, and
the slot lease is what actually guarantees reclamation.
"""

from __future__ import annotations

import shlex
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import structlog

from backend.workflow.domain.verify_stack import StackPlanError, derive_stack_plan

logger = structlog.get_logger(__name__)

#: Teardown is a `docker compose down` — seconds, not a build. Its own small
#: budget so a wedged teardown cannot eat the caller's whole verification.
_TEARDOWN_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class StackNotApplicable:
    """This product declares no verification stack — its repo is the environment."""


@dataclass(frozen=True)
class StackUnavailable:
    """The stack could not be stood up. An INFRASTRUCTURE fact, not a verdict."""

    reason: str


@dataclass(frozen=True)
class StackReady:
    """The stack is up under ``project``; probes may run against it."""

    project: str


StackOutcome = StackNotApplicable | StackUnavailable | StackReady


def _pinned(command: str, docker_context: str) -> str:
    """``command`` with docker's target pinned explicitly.

    ``docker``'s target is GLOBAL MUTABLE state: starting another colima profile
    flips the current context, and every unpinned call afterwards silently talks
    to the wrong VM (observed 2026-08-10 — ``docker ps`` reported "No such
    container" while production was perfectly healthy). A verification stack
    that inherits that is a non-deterministic environment, which is exactly the
    class of false result this whole track exists to remove.

    ``DOCKER_HOST`` OUTRANKS ``DOCKER_CONTEXT``, so a stray host var would make
    the pin a no-op — drop it rather than trust the caller's environment.
    """
    if not docker_context:
        # Not fail-closed: a single-daemon host is a legitimate deployment. But
        # it is named, because the failure it invites is silent.
        logger.warning("verify_stack_docker_context_unpinned")
        return command
    return f"env -u DOCKER_HOST DOCKER_CONTEXT={shlex.quote(docker_context)} {command}"


@asynccontextmanager
async def open_verification_stack(
    *,
    box: Any,
    slot_project: str,
    repo_files: Sequence[str],
    metadata: Mapping[str, Any] | None,
    docker_context: str,
    boot_timeout_s: float,
) -> AsyncIterator[StackOutcome]:
    """Bring one disposable instance up for the duration of the block.

    ``slot_project`` is the compose project name taken from the HELD slot — the
    caller must already own it, since the name is what ties a stack to a slot.
    """
    try:
        plan = derive_stack_plan(repo_files=repo_files, project=slot_project, metadata=metadata)
    except StackPlanError as exc:
        # Refusing to boot (e.g. no isolation overlay) is a safety decision, not
        # a verdict about the change.
        logger.warning("verify_stack_plan_refused", project=slot_project, reason=str(exc))
        yield StackUnavailable(reason=str(exc))
        return

    if plan is None:
        yield StackNotApplicable()
        return

    down = _pinned(plan.down, docker_context)
    up = _pinned(plan.up, docker_context)

    # Clear first: this slot's project may still hold a dead holder's leftovers,
    # and clearing them is precisely what makes "reclaim the slot" mean "reclaim
    # the stack" (#725). Idempotent — nothing there is a no-op.
    await box.exec(down, timeout_s=_TEARDOWN_TIMEOUT_S, shell=True)

    try:
        booted = await box.exec(up, timeout_s=boot_timeout_s, shell=True)
        if booted.timed_out or booted.exit_code != 0:
            detail = "\n".join(o for o in (booted.stdout, booted.stderr) if o)[-2000:]
            reason = (
                f"timed out after {boot_timeout_s}s"
                if booted.timed_out
                else f"exit {booted.exit_code}"
            )
            logger.warning("verify_stack_boot_failed", project=slot_project, reason=reason)
            yield StackUnavailable(reason=f"stack boot failed ({reason}): {detail}")
            return
        logger.info("verify_stack_ready", project=slot_project, source=plan.source)
        yield StackReady(project=slot_project)
    finally:
        # Best-effort: the slot lease is the real guarantee (a killed process
        # never reaches here), so a failed teardown is logged, never raised over
        # whatever the caller was doing.
        try:
            await box.exec(down, timeout_s=_TEARDOWN_TIMEOUT_S, shell=True)
        except Exception:  # noqa: BLE001 — teardown must not mask the caller's outcome
            logger.warning("verify_stack_teardown_failed", project=slot_project, exc_info=True)


__all__ = [
    "StackNotApplicable",
    "StackOutcome",
    "StackReady",
    "StackUnavailable",
    "open_verification_stack",
]
