"""The product's quick checks, run when the agent says it is done — before a round is spent.

A verification round runs the derived gate, the product's floor, the ledger judge and, last, the
product's final CI-equivalent run: many minutes. Prod #1259 (run 686d3874) spent one to find a
one-line type error its final ``mypy`` reported at the very end. A product that declares
``verify_precheck`` gets those commands run first, and a failure goes back to the agent as a
turn. Bounded: after :data:`MAX_PRECHECKS` asks in one drive, verification decides as before —
a check the agent cannot satisfy must not hold the run in a loop.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.workflow.application.gate_commands import (
    any_failed,
    product_declared_checks,
    run_gate_commands,
)
from backend.workflow.application.sandbox_provisioning import ensure_sandbox_ready
from backend.workflow.domain.verification_feedback import render_failed_commands
from backend.workflow.infrastructure.sandbox import SandboxSession

#: How many times one drive is sent back by the quick checks before verification decides.
MAX_PRECHECKS = 3


async def precheck_messages(
    session: AsyncSession, run: Any, box: SandboxSession, turn_text: str | None
) -> list[dict[str, Any]]:
    """The turn that hands back failed quick checks — ``[]`` when they pass or none exist."""
    commands = (await product_declared_checks(session, run.product_id)).precheck
    if not commands:
        return []
    venv_ready = await ensure_sandbox_ready(box)
    results = await run_gate_commands(
        box,
        [(c, "precheck") for c in commands],
        venv_bin=f"{box.workspace_mount}/.venv/bin" if venv_ready else None,
        timeout_s=get_settings().verify_gate_command_timeout_s,
    )
    if not any_failed(results):
        return []
    return [
        {"role": "assistant", "content": turn_text or "(done)"},
        {
            "role": "user",
            "content": (
                "QUICK CHECKS FAILED — the product's fast checks ran before verification and "
                "these failed. Fix them, then finish again; verification has not been spent.\n"
                f"{render_failed_commands(results)}"
            ),
        },
    ]


__all__ = ["MAX_PRECHECKS", "precheck_messages"]
