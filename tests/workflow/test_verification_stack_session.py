"""Standing one disposable instance up, driving it, and guaranteeing it goes away.

Composes the pieces built so far: a concurrency SLOT (#725) names the compose
project, a derived PLAN (#726) says how to boot, and the worker's exec channel
runs the commands on the founder's machine — outside the stack being verified,
so a broken stack produces an error rather than silence.

Three outcomes, fail-closed (the shape the derived gate already uses):

* not applicable — this product has no stack (a CLI; its repo IS the environment)
* unavailable    — no slot, or the boot failed. NOT a verification failure:
                   "could not stand it up" and "stood it up and the probe failed"
                   are different facts and must not be conflated.
* ready          — the stack is up; probes may run against it.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.workflow.infrastructure.sandbox import SandboxResult

pytestmark = pytest.mark.asyncio

_COMPOSE_FILES = ["deploy/compose.yaml", "deploy/compose.verify.yaml"]


class _Box:
    """Records every command the stack lifecycle dispatches to the worker."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, float]] = []
        self._fail_on = fail_on

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.calls.append((command, timeout_s))
        if self._fail_on and self._fail_on in command:
            return SandboxResult(exit_code=1, stdout="", stderr="boom", timed_out=False)
        return SandboxResult(exit_code=0, stdout="", stderr="", timed_out=False)

    @property
    def commands(self) -> list[str]:
        return [c for c, _ in self.calls]


def _open(box: Any, **over: Any) -> Any:
    from backend.workflow.application.verification_stack import open_verification_stack

    kwargs: dict[str, Any] = {
        "box": box,
        "slot_project": "verify-slot-0",
        "repo_files": _COMPOSE_FILES,
        "metadata": None,
        "docker_context": "colima",
        "boot_timeout_s": 1800.0,
    }
    kwargs.update(over)
    return open_verification_stack(**kwargs)


async def test_a_product_with_no_stack_is_not_applicable() -> None:
    from backend.workflow.application.verification_stack import StackNotApplicable

    box = _Box()
    async with _open(box, repo_files=["pyproject.toml"]) as outcome:
        assert isinstance(outcome, StackNotApplicable)
    assert box.commands == [], "nothing to boot means nothing dispatched"


async def test_ready_boots_after_clearing_the_slot_first() -> None:
    """The slot's project may hold a dead holder's leftovers — clearing FIRST is
    what makes reclaiming the slot equal reclaiming the stack (#725)."""
    from backend.workflow.application.verification_stack import StackReady

    box = _Box()
    async with _open(box) as outcome:
        assert isinstance(outcome, StackReady)
        assert outcome.project == "verify-slot-0"
        # cleanup-then-boot, in that order
        assert "down" in box.commands[0]
        assert " up " in f" {box.commands[1]} "

    # ...and torn down on the way out
    assert "down" in box.commands[-1]
    assert "-v" in box.commands[-1]


async def test_teardown_runs_even_when_the_body_raises() -> None:
    from backend.workflow.application.verification_stack import StackReady

    box = _Box()
    with pytest.raises(RuntimeError, match="probe blew up"):
        async with _open(box) as outcome:
            assert isinstance(outcome, StackReady)
            raise RuntimeError("probe blew up")

    assert "down" in box.commands[-1], "a raising probe must not leak the stack"


async def test_a_failed_boot_is_unavailable_not_a_verification_failure() -> None:
    from backend.workflow.application.verification_stack import StackUnavailable

    box = _Box(fail_on="up -d")
    async with _open(box) as outcome:
        assert isinstance(outcome, StackUnavailable)
        assert "boom" in outcome.reason
    assert "down" in box.commands[-1], "a half-booted stack must still be cleared"


async def test_commands_pin_the_docker_context_and_drop_docker_host() -> None:
    """``docker``'s target is global mutable state — another colima profile
    starting flips it. And ``DOCKER_HOST`` OUTRANKS ``DOCKER_CONTEXT``, so
    pinning the context while a stray host var leaks in is silently ignored."""
    box = _Box()
    async with _open(box):
        pass

    for cmd in box.commands:
        assert "DOCKER_CONTEXT=colima" in cmd
        assert "-u DOCKER_HOST" in cmd


async def test_boot_gets_the_boot_budget_not_the_gate_budget() -> None:
    """A cold image build is minutes; charging it to a per-command gate budget
    would turn a slow build into a false verification failure."""
    box = _Box()
    async with _open(box, boot_timeout_s=1234.0):
        pass

    boot = next(t for c, t in box.calls if " up " in f" {c} ")
    assert boot == 1234.0
