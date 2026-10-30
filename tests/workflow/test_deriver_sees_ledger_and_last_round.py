"""The gate deriver neither re-checks the ledger by text search nor moves its own goalposts.

Prod #1275 (run 35f3ac0f, 2026-10-30): three of four rounds failed on ``kind:quality`` commands
the deriver wrote fresh each round — ``git diff … | grep -q 'build_run_detail'``, ``… | grep -qi
'requirement'`` — turning the issue's own guard items into keyword searches. The ledger already
checks those items (cited evidence, a judge reading the cited test), and each round's grep was
worded differently, so a check that passed one round was replaced by a stricter one the next.

So the deriver is shown the ledger items, told not to re-check them by text search, and shown
the commands the previous verification ran, to re-emit unless the change made one wrong.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.workflow.domain.gate_derivation import (
    DerivedCommand,
    DerivedGate,
    derivation_planner_messages,
)
from backend.workflow.domain.requirements import REQUIREMENTS_KEY
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession

_LEDGER = [
    {"id": "R1", "text": "runs_detail lists the ledger rows", "kind": "do"},
    {"id": "R2", "text": "an old item", "kind": "do", "withdrawn": True},
]


def test_the_deriver_is_shown_the_ledger_and_told_not_to_grep_for_it() -> None:
    system, user = derivation_planner_messages(
        manifests={},
        changed_files=[],
        intent="x",
        ledger_items=("runs_detail lists the ledger rows",),
    )
    assert "LEDGER ITEMS" in system["content"]
    assert "text search" in system["content"]
    assert "runs_detail lists the ledger rows" in user["content"]


def test_the_deriver_is_shown_last_rounds_commands_to_keep_them() -> None:
    system, user = derivation_planner_messages(
        manifests={},
        changed_files=[],
        intent="x",
        previous_commands=("make check",),
    )
    assert "PREVIOUS ROUND" in system["content"]
    assert "VERBATIM" in system["content"]
    assert "make check" in user["content"]


def test_without_them_the_prompt_says_nothing_about_them() -> None:
    _, user = derivation_planner_messages(manifests={}, changed_files=[], intent="x")
    assert "ledger" not in user["content"].lower()
    assert "previous verification" not in user["content"].lower()


async def _spy_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Any:
    from backend.workflow.application.verification_service import VerificationService
    from tests._support import memory_session
    from tests.execution.test_verification_service import StubLlm, _make_run

    seen: dict[str, Any] = {}

    async def author(*_a: Any, **kw: Any) -> DerivedGate:
        seen.update(kw)
        return DerivedGate(applicable=True, commands=(DerivedCommand(command="true"),))

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        monkeypatch.setattr(svc, "_author_derived_gate", author)
        monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        run.payload = {**run.payload, REQUIREMENTS_KEY: _LEDGER}
        await svc._run_derived_gate(run, NoopSandboxSession(str(tmp_path)), [], **kwargs)
    return seen


async def test_the_server_gate_hands_the_live_ledger_items_to_the_deriver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = await _spy_gate(tmp_path, monkeypatch)
    assert seen["ledger_items"] == ("runs_detail lists the ledger rows",)  # not the withdrawn one


async def test_the_server_gate_hands_the_previous_commands_to_the_deriver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = await _spy_gate(tmp_path, monkeypatch, previous=("make check",))
    assert seen["previous_commands"] == ("make check",)


async def test_verify_reads_the_previous_rounds_derived_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two verifications of one run: the second deriver call sees what the first one ran —
    its derived commands only, never the product's floor/final."""
    import backend.storage.product_workspace as product_workspace
    from backend.identity.workspaces_db import ProductRow
    from backend.workflow.application.verification_service import VerificationService
    from backend.workflow.domain.declared_checks import FLOOR_KEY
    from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
    from tests._support import memory_session
    from tests.execution.test_verification_service import (
        StubLlm,
        _make_run,
        _make_step_and_attempt,
    )

    async def _noop(*_a: Any, **_k: Any) -> Any:
        return type("_Merge", (), {"status": "clean", "conflict_paths": []})()

    monkeypatch.setattr(product_workspace, "commit_worktree", _noop)
    monkeypatch.setattr(product_workspace, "merge_main_into_worktree", _noop)
    calls: list[dict[str, Any]] = []

    async def author(*_a: Any, **kw: Any) -> DerivedGate:
        calls.append(kw)
        return DerivedGate(
            applicable=True, commands=(DerivedCommand(command="test -d .", kind="quality"),)
        )

    async with memory_session() as session:
        run = await _make_run(session)
        product = ProductRow(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            name="P",
            slug=f"p-{uuid.uuid4().hex[:6]}",
            product_metadata={FLOOR_KEY: ["true"]},
        )
        session.add(product)
        run.product_id = product.id
        await session.flush()
        step, attempt = await _make_step_and_attempt(session, run)
        svc = VerificationService(session=session, llm=StubLlm([]))
        monkeypatch.setattr(svc, "_author_derived_gate", author)
        monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
        monkeypatch.setattr(svc, "_manifest_present", lambda r: True)

        async def _no_scope(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(svc, "_run_scope_check", _no_scope)
        for _ in range(2):
            await svc.verify(
                run=run,
                work_step=step,
                attempt=attempt,
                contract=VerificationContract(
                    checks=(VerificationCheck(kind="command", command="true"),)
                ),
                box=NoopSandboxSession(str(tmp_path)),
                written_paths=[],
                final_text="done",
            )
            await session.flush()

    assert "previous_commands" not in calls[0]
    assert calls[1]["previous_commands"] == ("test -d .",)
