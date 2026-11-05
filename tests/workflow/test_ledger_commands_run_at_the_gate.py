"""A ledger item that asks for commands to pass is decided by running them (#1316).

Prod #1316 (run 26701cf3, 2026-11-05, PWA only): R5 read "`npx --yes pnpm@9 lint`,
`… exec tsc --noEmit`, `… test` 통과". The gate ran none of the three — the item went to a judge
that reads the agent's cited evidence — so R5 was ``rejected`` in round 1 and ``unclear`` in
round 2, and only the PR's CI said whether they passed.

These tests build the ledger the way intake does, run the server gate over a PWA-only change
and judge the ledger the way verification does; ``npx`` is a stub on ``PATH`` whose exit code
the test chooses, so what is checked is that the exit code decides the item.
"""

from __future__ import annotations

import stat
import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.workflow.domain.gate_derivation import (
    DerivedCommand,
    DerivedGate,
    derivation_planner_messages,
)
from backend.workflow.domain.ledger_commands import commands_required
from backend.workflow.domain.requirements import REQUIREMENTS_KEY, build_ledger
from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession

_ISSUE = """## 요구사항

- [ ] PWA 의 `npx --yes pnpm@9 lint`, `… exec tsc --noEmit`, `… test` 통과
- [ ] 설정 화면이 저장 버튼을 보여준다
"""
_PWA = ["apps/pwa/src/settings.tsx"]
_COMMANDS = (
    "npx --yes pnpm@9 lint",
    "npx --yes pnpm@9 exec tsc --noEmit",
    "npx --yes pnpm@9 test",
)


def _fake_npx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failing: str = "") -> None:
    """``npx`` that fails exactly when its arguments end with ``failing``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "npx"
    script.write_text(
        f'#!/bin/sh\ncase "$*" in *"{failing}") [ -n "{failing}" ] && exit 1;; esac\nexit 0\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


async def _gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failing: str = ""
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Run the server gate then the ledger verdict; return (deriver kwargs, gate blob, rows)."""
    from backend.workflow.application.verification_service import VerificationService
    from tests._support import memory_session
    from tests.execution.test_verification_service import StubLlm, _make_run

    _fake_npx(tmp_path, monkeypatch, failing=failing)
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
        run.payload = {
            **run.payload,
            "intent_text": _ISSUE,
            "github_event": "issues",
            REQUIREMENTS_KEY: build_ledger({"intent_text": _ISSUE, "github_event": "issues"}),
        }
        box = NoopSandboxSession(str(tmp_path))
        outcome = await svc._run_derived_gate(run, box, _PWA)
        blob = outcome.blob  # type: ignore[union-attr]
        rows = await svc._judge_requirements(
            run,
            VerificationContract(checks=(VerificationCheck(kind="command", command="true"),)),
            _PWA,
            box,
            None,
            [],
            blob,
            judge=False,
        )
    assert rows is not None
    return seen, blob, rows


def test_the_issue_text_is_read_as_three_commands_with_the_abbreviation_expanded() -> None:
    required = commands_required(build_ledger({"intent_text": _ISSUE})[0]["text"])
    assert required is not None
    assert required.commands == _COMMANDS
    assert required.unresolved == ()


def test_an_abbreviation_with_no_prefix_is_unclear() -> None:
    from backend.workflow.domain.ledger_commands import command_verdict

    required = commands_required("`… test` 통과")
    assert required is not None and required.commands == ()
    status, note = command_verdict(required, [])
    assert status == "unclear"
    assert "`… test`" in note


def test_prose_and_file_names_are_not_commands() -> None:
    assert commands_required("`settings.py` 의 저장 버튼 통과") is None
    assert commands_required("`pytest` 를 돌린다") is None  # no pass requirement


async def test_the_pwa_commands_join_the_gate_run_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, blob, _ = await _gate(tmp_path, monkeypatch)
    ran = {c["command"]: c for c in blob["commands"]}
    assert set(_COMMANDS) <= set(ran)
    assert all(ran[c]["kind"] == "ledger" and ran[c]["status"] == "passed" for c in _COMMANDS)
    # The deriver is told they run anyway, so it does not emit them a second time.
    assert seen["ledger_commands"] == _COMMANDS


async def test_other_items_keep_the_no_text_search_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, _, _ = await _gate(tmp_path, monkeypatch)
    assert seen["ledger_items"] == ("설정 화면이 저장 버튼을 보여준다",)
    system, user = derivation_planner_messages(
        manifests={}, changed_files=_PWA, intent="x", ledger_items=seen["ledger_items"]
    )
    assert "text search" in system["content"]
    assert "설정 화면이 저장 버튼을 보여준다" in user["content"]
    assert "npx" not in user["content"]


async def test_exit_codes_decide_the_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, rows = await _gate(tmp_path / "ok", monkeypatch)
    assert rows[0]["status"] == "done"


async def test_a_failing_command_rejects_the_item_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, blob, rows = await _gate(tmp_path / "bad", monkeypatch, failing="exec tsc --noEmit")
    assert rows[0]["status"] == "rejected"
    assert "npx --yes pnpm@9 exec tsc --noEmit" in rows[0]["note"]
    assert "npx --yes pnpm@9 lint" not in rows[0]["note"]
    assert blob["passed"] is False
