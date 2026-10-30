"""Running a gate's commands — one runner for both execution models.

The server gate and the in-place gate used to carry a copy each of this loop. They must not
differ in what a command's result means, and they must not differ in the conditions it runs
under (``CI=true``, the project venv) — so both run their commands here.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.workflow.domain.declared_checks import (
    DeclaredChecks,
    gate_command,
    read_declared_checks,
)
from backend.workflow.infrastructure.sandbox import SandboxSession

#: ``command not found``: the tool is not on that machine — recorded, never a false-fail.
_MISSING_TOOL_EXIT = 127


async def run_gate_commands(
    box: SandboxSession,
    commands: Sequence[tuple[str, str]],
    *,
    venv_bin: str | None,
    timeout_s: float,
) -> list[dict[str, Any]]:
    """Run each ``(command, kind)`` in ``box``; the exit code is the verdict."""
    results: list[dict[str, Any]] = []
    for command, kind in commands:
        # S604: the sandbox Protocol's own flag, not a subprocess — the command is the repo's
        # declared check, which is the whole point of the gate.
        res = await box.exec(  # noqa: S604
            gate_command(command, venv_bin), timeout_s=timeout_s, shell=True
        )
        if res.exit_code == 0 and not res.timed_out:
            status = "passed"
        elif res.exit_code == _MISSING_TOOL_EXIT:
            status = "unavailable"
        else:
            status = "failed"
        results.append(
            {
                "command": command,
                "kind": kind,
                "status": status,
                "exit_code": res.exit_code,
                "timed_out": res.timed_out,
                "output": "\n".join(o for o in (res.stdout, res.stderr) if o)[-2000:],
            }
        )
    return results


def any_failed(results: Sequence[dict[str, Any]]) -> bool:
    return any(r.get("status") == "failed" for r in results)


async def product_declared_checks(
    session: AsyncSession, product_id: uuid.UUID | None
) -> DeclaredChecks:
    """The product's ``verify_floor`` / ``verify_final``. Empty for a run with no product."""
    if product_id is None:
        return DeclaredChecks()
    from sqlalchemy import select  # noqa: PLC0415

    from backend.identity.workspaces_db import ProductRow  # noqa: PLC0415

    row = (
        await session.execute(
            select(ProductRow.product_metadata).where(ProductRow.id == product_id)
        )
    ).first()
    return read_declared_checks(row[0] if row else None)


#: The kinds the DERIVER wrote — never the product's floor/final or the agent's declared ones.
_DERIVED_KINDS = frozenset({"quality", "test", "surface"})


async def latest_derived_commands(
    session: AsyncSession, run_id: uuid.UUID | None
) -> tuple[str, ...]:
    """The commands the deriver produced for this run's latest verification (``()`` if none).

    Shown to the next round's deriver so it re-emits them instead of re-wording them (#1275)."""
    if run_id is None:
        return ()
    from sqlalchemy import select  # noqa: PLC0415

    from backend.workflow.infrastructure.db import VerificationResult  # noqa: PLC0415

    result = (
        await session.execute(
            select(VerificationResult.result)
            .where(VerificationResult.run_id == run_id)
            .order_by(VerificationResult.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    gate = result.get("derived_gate") if isinstance(result, dict) else None
    commands = gate.get("commands") if isinstance(gate, dict) else None
    if not isinstance(commands, list):
        return ()
    return tuple(
        str(c["command"])
        for c in commands
        if isinstance(c, dict) and c.get("kind") in _DERIVED_KINDS and c.get("command")
    )


__all__ = [
    "any_failed",
    "latest_derived_commands",
    "product_declared_checks",
    "run_gate_commands",
]
