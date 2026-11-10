"""A ledger item that asks for commands to pass is decided by running them (#1316).

Prod #1316 (run 26701cf3, 2026-11-05, PWA only): R5 read "`npx --yes pnpm@9 lint`,
`… exec tsc --noEmit`, `… test` 통과". The gate ran none of the three — the item went to a judge
that reads the agent's cited evidence — so R5 was ``rejected`` in round 1 and ``unclear`` in
round 2, and only the PR's CI said whether they passed.

The request's string cannot simply be run: the repo root has no ``package.json`` (the PWA is
``apps/pwa``), so ``npx … lint`` fails there for ever - a failure the agent cannot fix. The
deriver is handed the commands and answers with the repo's runnable form of each; the item is
decided from those exit codes, and left ``unclear`` when no runnable form exists.

These tests build the ledger the way intake does, run the server gate over a PWA-only change
and judge the ledger the way verification does; ``npx`` is a stub on ``PATH`` that needs a
``package.json`` in its working directory and whose exit code the test chooses.
"""

from __future__ import annotations

import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from backend.workflow.domain.gate_derivation import (
    DerivedCommand,
    DerivedGate,
    derivation_planner_messages,
)
from backend.workflow.domain.ledger_commands import (
    command_verdict,
    commands_required,
    covers_command,
    covers_rejection,
)
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
    """``npx`` as it behaves in this repo: it needs a ``package.json`` in the directory it runs
    in (the root has none, ``apps/pwa`` has) and it fails when its arguments end with
    ``failing``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "npx"
    script.write_text(
        "#!/bin/sh\n"
        '[ -f package.json ] || { echo "no package.json in $PWD" >&2; exit 1; }\n'
        f'case "$*" in *"{failing}") [ -n "{failing}" ] && exit 1;; esac\n'
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    pwa = tmp_path / "apps" / "pwa"
    pwa.mkdir(parents=True)
    (pwa / "package.json").write_text("{}")


async def _gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    failing: str = "",
    issue: str = _ISSUE,
    runnable_form: bool = True,
    form: Callable[[str], str] = lambda c: f"cd apps/pwa && {c}",
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Run the server gate then the ledger verdict; return (deriver kwargs, gate blob, rows).

    The deriver is stubbed, but it answers from what the real one is handed: the repo's
    runnable form (``cd apps/pwa && ...``) of each ledger command, tagged ``covers``.
    ``runnable_form=False`` is a deriver that cannot tell where to run them.
    """
    from backend.workflow.application.verification_service import VerificationService
    from tests._support import memory_session
    from tests.execution.test_verification_service import StubLlm, _make_run

    _fake_npx(tmp_path, monkeypatch, failing=failing)
    seen: dict[str, Any] = {}

    async def author(*_a: Any, **kw: Any) -> DerivedGate:
        seen.update(kw)
        derived = [DerivedCommand(command="true")]
        if runnable_form:
            derived += [
                DerivedCommand(command=form(c), covers=(c,)) for c in kw.get("ledger_commands", ())
            ]
        return DerivedGate(applicable=True, commands=tuple(derived))

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        monkeypatch.setattr(svc, "_author_derived_gate", author)
        monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        run.payload = {
            **run.payload,
            "intent_text": issue,
            "github_event": "issues",
            REQUIREMENTS_KEY: build_ledger({"intent_text": issue, "github_event": "issues"}),
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
    required = commands_required("`… test` 통과")
    assert required is not None and required.commands == ()
    status, note = command_verdict(required, [])
    assert status == "unclear"
    assert "`… test`" in note


def test_the_prefix_is_lent_only_by_a_package_script() -> None:
    # The last word of these is a path or a tool, not a sub-command: `… ruff check` would be
    # a guess (`uv run pytest ruff check`), so it stays unresolved.
    for first in ("uv run pytest tests/x.py", "uv run pytest", "cargo test"):
        required = commands_required(f"`{first}`, `… ruff check` 통과")
        assert required is not None and required.commands == (first,)
        assert required.unresolved == ("… ruff check",)


def test_prose_and_file_names_are_not_commands() -> None:
    assert commands_required("`settings.py` 의 저장 버튼 통과") is None
    assert commands_required("`pytest` 를 돌린다") is None  # no pass requirement


async def test_the_pwa_commands_join_the_gate_run_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen, blob, _ = await _gate(tmp_path, monkeypatch)
    ran = {c["command"]: c for c in blob["commands"]}
    # Handed to the deriver to turn into the repo's runnable form ...
    assert seen["ledger_commands"] == _COMMANDS
    # ... and run where the repo runs them, tagged with the request's wording.
    for command in _COMMANDS:
        result = ran[f"cd apps/pwa && {command}"]
        assert result["status"] == "passed" and result["covers"] == [command]
    # The request's string is never run as written: at the repo root it cannot work.
    assert not set(_COMMANDS) & set(ran)


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


async def test_a_command_with_no_runnable_form_is_never_run_at_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as written, ``npx`` fails at the repo root for ever - a failure the agent cannot fix.
    A deriver that cannot place them runs nothing at the root, and the gate has no verdict:
    the judge's stands, with a note saying the gate found nothing to run."""
    _, blob, rows = await _gate(tmp_path / "x", monkeypatch, runnable_form=False)
    # The judge's path (``check_ledger`` finds no evidence), not a command verdict.
    assert rows[0]["status"] == "missing"
    assert "게이트가 실행 형태를 찾지 못함" in rows[0]["note"]
    assert "npx --yes pnpm@9 lint" in rows[0]["note"]
    assert [c["command"] for c in blob["commands"]] == ["true"]
    assert blob["passed"] is True


async def test_a_non_command_span_leaves_the_judges_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = "- [ ] `failed rounds` 가 0이면 통과\n"
    _, _, rows = await _gate(tmp_path / "s", monkeypatch, issue=issue, runnable_form=False)
    assert rows[0]["status"] not in {"unclear", "done", "rejected"}
    assert "게이트가 실행 형태를 찾지 못함: `failed rounds`" in rows[0]["note"]


async def test_a_covers_on_another_command_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, blob, rows = await _gate(tmp_path / "e", monkeypatch, form=lambda _c: "echo ok")
    assert all("covers" not in c for c in blob["commands"])
    assert rows[0]["status"] != "done"


async def test_a_covers_on_the_real_command_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, blob, rows = await _gate(tmp_path / "r", monkeypatch)
    assert any(c.get("covers") == ["npx --yes pnpm@9 lint"] for c in blob["commands"])
    assert rows[0]["status"] == "done"


def test_covers_command_compares_the_substance_in_order() -> None:
    assert covers_command("cd apps/pwa && npx --yes pnpm@9 lint", "npx --yes pnpm@9 lint")
    assert covers_command("pnpm exec tsc --noEmit", "npx --yes pnpm@9 exec tsc --noEmit")
    assert covers_command("tsc --noEmit", "npx --yes pnpm@9 exec tsc --noEmit")
    assert covers_command("pnpm run lint", "pnpm lint")
    assert not covers_command("echo ok", "npx --yes pnpm@9 lint")
    assert not covers_command("--noEmit tsc", "npx --yes pnpm@9 exec tsc --noEmit")


_LINT = "npx --yes pnpm@9 lint"


def test_covers_command_wants_the_core_in_the_command_position() -> None:
    assert covers_command("cd apps/pwa && npx --yes pnpm@9 lint", _LINT)
    assert covers_command("pnpm run lint", _LINT)
    assert covers_command(
        "cd apps/pwa && pnpm exec tsc --noEmit", "npx --yes pnpm@9 exec tsc --noEmit"
    )
    assert covers_command("tsc --noEmit", "npx --yes pnpm@9 exec tsc --noEmit")
    test = "npx --yes pnpm@9 test"
    assert not covers_command("echo test", test)
    assert not covers_command("cd apps/pwa && ls test", test)
    assert not covers_command("grep lint package.json", _LINT)


def test_covers_command_refuses_a_form_that_hides_the_exit_code() -> None:
    for hidden in (
        "cd apps/pwa && npx --yes pnpm@9 lint || true",
        "cd apps/pwa && npx --yes pnpm@9 lint; true",
        "cd apps/pwa && npx --yes pnpm@9 lint | tee out.txt",
        "cd apps/pwa && npx --yes pnpm@9 lint && true",
        "cd apps/pwa && npx --yes pnpm@9 lint && exit 0",
        "cd apps/pwa && npx --yes pnpm@9 lint && :",
    ):
        assert not covers_command(hidden, _LINT), hidden
    assert covers_command("cd apps/pwa && npx --yes pnpm@9 lint && echo done", _LINT)


def test_covers_command_refuses_an_operator_after_the_command_that_hides_its_exit_code() -> None:
    for hidden in (
        "pnpm lint && echo done || echo x",
        "pnpm lint && echo done; echo x",
        "pnpm lint && echo done | tee out.txt",
    ):
        assert covers_rejection(hidden, "pnpm lint") is not None, hidden
    reason = covers_rejection("pnpm lint && echo done || echo x", "pnpm lint")
    assert reason is not None and "||" in reason


def test_covers_command_refuses_a_background_ampersand() -> None:
    for hidden in (
        "pnpm lint &",
        "pnpm lint & wait",
        "pnpm lint && pnpm test &",
        "sleep 1 & pnpm lint",
    ):
        reason = covers_rejection(hidden, "pnpm lint")
        assert reason is not None and "&" in reason, hidden


def test_covers_command_keeps_accepting_the_ordinary_forms() -> None:
    assert covers_rejection("cd apps/pwa && npx --yes pnpm@9 lint", "pnpm lint") is None
    assert covers_rejection("pnpm install && pnpm lint && pnpm test", "pnpm lint") is None
    assert covers_rejection("CI=true uv run pytest tests/x 2>&1", "uv run pytest tests/x") is None
    assert covers_rejection("pnpm lint >&2", "pnpm lint") is None
    assert covers_rejection("pnpm lint &> out.txt", "pnpm lint") is None


async def test_a_covers_that_hides_the_failure_is_dropped_at_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, blob, rows = await _gate(
        tmp_path / "h",
        monkeypatch,
        failing="lint",
        form=lambda c: f"cd apps/pwa && {c} || true",
    )
    assert all("covers" not in c for c in blob["commands"])
    assert rows[0]["status"] != "done"


async def test_an_abbreviation_that_would_be_a_guess_stays_unclear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = "- [ ] `uv run pytest tests/x.py`, `… ruff check` 통과\n"
    seen, _, rows = await _gate(tmp_path / "g", monkeypatch, issue=issue)
    assert seen["ledger_commands"] == ("uv run pytest tests/x.py",)
    assert rows[0]["status"] == "unclear"
    assert "`… ruff check`" in rows[0]["note"]
    assert "uv run pytest ruff check" not in str(seen) + str(rows)
