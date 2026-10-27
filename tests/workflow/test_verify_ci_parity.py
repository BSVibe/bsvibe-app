"""Verification runs the way the repo's CI runs — and runs what the product declares.

Prod #1204 (run 2538f8bc, 2026-10-26): every gate round failed on
``ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY … set the CI environment variable to "true"``.
CI has ``CI=true``; the sandbox did not, so the same command meant different things in the two
places, and no amount of work on the change could make the gate pass.

And the gate is DERIVED by a model each round. What a product needs checked every time (its
architecture guards, the suite CI runs before merge) cannot depend on a model remembering it.
So a product declares two lists in its metadata, and the platform runs them:

* ``verify_floor`` — on every verification, beside the derived commands;
* ``verify_final`` — once, after everything else passed: the CI-equivalent full run, so a
  change that would turn CI red fails HERE, inside the round, instead of after delivery.

Every command below runs through a real shell (``NoopSandboxSession``), so ``CI`` is what the
command actually saw — not what a stub recorded.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from backend.identity.workspaces_db import ProductRow
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.declared_checks import (
    FINAL_KEY,
    FLOOR_KEY,
    gate_command,
    read_declared_checks,
    validate_declared_checks,
)
from backend.workflow.domain.gate_derivation import DerivedCommand, DerivedGate
from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
from backend.workflow.infrastructure.db import VerificationOutcome
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession
from tests._support import memory_session
from tests.execution.test_verification_service import (
    StubLlm,
    _make_run,
    _make_step_and_attempt,
)

#: Exits 0 only when the command ran with CI=true.
_SEES_CI = 'test "$CI" = true'


#: What the last stubbed deriver was handed — the product checks it was told about.
_AUTHOR_KWARGS: dict[str, Any] = {}


def _gate(*commands: str) -> Any:
    async def author(*_a: Any, **kwargs: Any) -> DerivedGate:
        _AUTHOR_KWARGS.clear()
        _AUTHOR_KWARGS.update(kwargs)
        return DerivedGate(
            applicable=True,
            commands=tuple(DerivedCommand(command=c, kind="test") for c in commands),
        )

    return author


def _service(session: Any, monkeypatch: pytest.MonkeyPatch, *commands: str) -> Any:
    svc = VerificationService(session=session, llm=StubLlm([]))
    monkeypatch.setattr(svc, "_author_derived_gate", _gate(*commands))
    monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
    return svc


# ── the product's declaration ────────────────────────────────────────────────


def test_declared_checks_are_read_from_product_metadata() -> None:
    checks = read_declared_checks(
        {FLOOR_KEY: ["uv run lint-imports", "  "], FINAL_KEY: ["uv run pytest"]}
    )
    assert checks.floor == ("uv run lint-imports",)
    assert checks.final == ("uv run pytest",)
    assert read_declared_checks({}).floor == ()
    assert read_declared_checks(None).final == ()


@pytest.mark.parametrize(
    "bad",
    [
        {FLOOR_KEY: "uv run pytest"},  # a string, not a list
        {FINAL_KEY: [1]},
        {FLOOR_KEY: [""]},
    ],
)
def test_a_malformed_declaration_is_refused_at_the_write(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        validate_declared_checks(bad)


def test_the_write_seam_refuses_a_malformed_declaration() -> None:
    from backend.workflow.application.product_secrets import sealed_product_metadata

    with pytest.raises(ValueError):
        sealed_product_metadata({FLOOR_KEY: "uv run pytest"}, {})
    assert sealed_product_metadata({FLOOR_KEY: ["make check"]}, {})[FLOOR_KEY] == ["make check"]


def test_a_gate_command_carries_ci() -> None:
    assert "CI=true" in gate_command("pnpm install", None)
    assert gate_command("x", "/w/.venv/bin").endswith("x")


# ── CI=true, as each command saw it ──────────────────────────────────────────


async def test_a_derived_gate_command_runs_with_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with memory_session() as session:
        svc = _service(session, monkeypatch, _SEES_CI)
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        gate = await svc._run_derived_gate(run, NoopSandboxSession(str(tmp_path)), [])
    assert gate.blob["passed"] is True, gate.blob["commands"]


async def test_a_declared_command_check_runs_with_ci(tmp_path: Path) -> None:
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        contract = VerificationContract(
            checks=(VerificationCheck(kind="command", command=_SEES_CI),)
        )
        [result] = await svc._run_command_checks(contract, NoopSandboxSession(str(tmp_path)))
    assert result["passed"] is True, result


async def test_an_in_place_gate_command_runs_with_ci(tmp_path: Path) -> None:
    from backend.workflow.application.inplace_gate import _run_commands

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        gate = DerivedGate(applicable=True, commands=(DerivedCommand(command=_SEES_CI),))
        [result] = await _run_commands(svc, gate=gate, box=NoopSandboxSession(str(tmp_path)))
    assert result["status"] == "passed", result


# ── the floor ────────────────────────────────────────────────────────────────


async def test_the_floor_runs_beside_the_derived_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with memory_session() as session:
        svc = _service(session, monkeypatch, "true")
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        gate = await svc._run_derived_gate(
            run, NoopSandboxSession(str(tmp_path)), [], floor=("exit 3",)
        )
    floor = [c for c in gate.blob["commands"] if c["kind"] == "floor"]
    assert [c["command"] for c in floor] == ["exit 3"]
    assert floor[0]["status"] == "failed"
    assert gate.blob["passed"] is False


async def test_the_floor_runs_even_when_the_deriver_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with memory_session() as session:
        svc = _service(session, monkeypatch)  # an empty derived gate
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        gate = await svc._run_derived_gate(
            run, NoopSandboxSession(str(tmp_path)), [], floor=(_SEES_CI,)
        )
    assert [c["kind"] for c in gate.blob["commands"]] == ["floor"]
    assert gate.blob["passed"] is True, gate.blob["commands"]


# ── through verify(): the product's metadata decides ─────────────────────────


async def _verify_product_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, Any],
    *,
    derived: tuple[str, ...] = ("true",),
) -> Any:
    import backend.storage.product_workspace as product_workspace

    async def _committed(*_a: Any, **_k: Any) -> None:
        return None

    async def _merged(*_a: Any, **_k: Any) -> Any:
        return type("_Merge", (), {"status": "clean", "conflict_paths": []})()

    monkeypatch.setattr(product_workspace, "commit_worktree", _committed)
    monkeypatch.setattr(product_workspace, "merge_main_into_worktree", _merged)
    marker = tmp_path / "ran"
    async with memory_session() as session:
        run = await _make_run(session)
        product = ProductRow(
            id=uuid.uuid4(),
            workspace_id=run.workspace_id,
            name="P",
            slug=f"p-{uuid.uuid4().hex[:6]}",
            product_metadata=metadata,
        )
        session.add(product)
        run.product_id = product.id
        await session.flush()
        step, attempt = await _make_step_and_attempt(session, run)
        svc = _service(session, monkeypatch, *derived)
        monkeypatch.setattr(svc, "_manifest_present", lambda r: True)

        async def _no_scope(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(svc, "_run_scope_check", _no_scope)
        vr = await svc.verify(
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
    return vr, marker


async def test_the_final_check_runs_once_everything_else_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vr, _ = await _verify_product_run(
        tmp_path, monkeypatch, {FINAL_KEY: [f"{_SEES_CI} && touch ran"]}
    )
    final = [c for c in vr.result["derived_gate"]["commands"] if c["kind"] == "final"]
    assert [c["status"] for c in final] == ["passed"]
    assert (tmp_path / "ran").exists()
    assert vr.outcome is VerificationOutcome.PASSED


async def test_a_failing_final_check_fails_the_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vr, _ = await _verify_product_run(tmp_path, monkeypatch, {FINAL_KEY: ["exit 1"]})
    assert vr.outcome is VerificationOutcome.FAILED
    assert vr.result["derived_gate"]["passed"] is False

    from backend.workflow.domain.verification_feedback import render_verification_failure

    assert "exit 1" in render_verification_failure(vr.result)


async def test_the_final_check_is_not_spent_on_a_run_that_already_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vr, _ = await _verify_product_run(
        tmp_path, monkeypatch, {FINAL_KEY: ["touch ran"]}, derived=("false",)
    )
    assert vr.outcome is VerificationOutcome.FAILED
    assert not (tmp_path / "ran").exists()
    assert all(c["kind"] != "final" for c in vr.result["derived_gate"]["commands"])


async def test_the_floor_is_read_from_the_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vr, _ = await _verify_product_run(tmp_path, monkeypatch, {FLOOR_KEY: ["exit 4"]})
    assert vr.outcome is VerificationOutcome.FAILED
    assert [
        c["command"] for c in vr.result["derived_gate"]["commands"] if c["kind"] == "floor"
    ] == ["exit 4"]


# ── the in-place gate (the founder's machine) runs the same declaration ─────


async def test_the_in_place_gate_runs_the_floor_and_then_the_final() -> None:
    from backend.workflow.application.inplace_gate import run_inplace_gate
    from backend.workflow.domain.declared_checks import DeclaredChecks
    from tests.workflow.test_verify_unified_across_models import _GATE_JSON, _Box, _Llm, _Run
    from tests.workflow.test_verify_unified_across_models import _service as _unified

    box = _Box(manifests={"pyproject.toml": "x"})
    blob = await run_inplace_gate(
        _unified(_Llm(_GATE_JSON)),
        run=_Run(),
        box=box,
        declared=DeclaredChecks(floor=("make guards",), final=("make ci",)),
    )

    assert blob is not None
    kinds = [c["kind"] for c in blob["commands"]]
    assert kinds[-2:] == ["floor", "final"], kinds
    assert blob["passed"] is True
    assert all("CI=true" in c for c in box.execs if "make" in c)


async def test_the_in_place_final_is_skipped_after_a_failure_and_fails_the_gate_itself() -> None:
    from backend.workflow.application.inplace_gate import run_inplace_gate
    from backend.workflow.domain.declared_checks import DeclaredChecks
    from tests.workflow.test_verify_unified_across_models import _GATE_JSON, _Box, _Llm, _Run
    from tests.workflow.test_verify_unified_across_models import _service as _unified

    failing_floor = _Box(manifests={"pyproject.toml": "x"}, exits={"make guards": 1})
    blob = await run_inplace_gate(
        _unified(_Llm(_GATE_JSON)),
        run=_Run(),
        box=failing_floor,
        declared=DeclaredChecks(floor=("make guards",), final=("make ci",)),
    )
    assert blob is not None and blob["passed"] is False
    assert not any("make ci" in c for c in failing_floor.execs)

    failing_final = _Box(manifests={"pyproject.toml": "x"}, exits={"make ci": 1})
    blob = await run_inplace_gate(
        _unified(_Llm(_GATE_JSON)),
        run=_Run(),
        box=failing_final,
        declared=DeclaredChecks(final=("make ci",)),
    )
    assert blob is not None and blob["passed"] is False and blob["proved"] is False


# ── what the deriver and the agent are told ──────────────────────────────────


def test_the_deriver_keeps_ci_checks_whole_and_on_cis_tool_versions() -> None:
    """#1204's gate re-derived ``npx pnpm@10 install`` while CI pins pnpm 10.33.0, and the old
    prompt told it to narrow every CI check by the changed-files rule."""
    from backend.workflow.domain.gate_derivation import _DERIVATION_SYSTEM_PROMPT as prompt

    ci_line = next(line for line in prompt.splitlines() if "CI DECLARATIONS" in line)
    assert "lint / format step is narrowed" in ci_line
    assert "type check, build or guard step is NOT narrowed" in ci_line
    assert "never only the changed test file" in prompt
    assert "exact version the CI configuration pins" in prompt


def test_the_agent_scopes_lint_but_not_tests() -> None:
    from backend.mcp.tools.work_tools import VerificationCheck as DeclareVerificationCheck
    from backend.workflow.infrastructure.tools import VERIFY_FIRST_REFUSAL

    field = DeclareVerificationCheck.model_fields["command"].description or ""
    assert "never the whole repo" not in field
    assert "lint" in field and "test" in field
    assert "scoped to the files you change" not in VERIFY_FIRST_REFUSAL


async def test_settling_a_client_attach_run_hands_the_products_declaration_to_its_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from backend.workflow.application import (
        _loop_context,
        gate_commands,
        inplace_gate,
        verify_environment,
    )
    from backend.workflow.domain.declared_checks import DeclaredChecks
    from tests.workflow.test_verify_environment import (
        CheckEnvironmentStub,
        _Attempt,
        _Box,
        _Orch,
        _Run,
        _Step,
    )

    product_checks = DeclaredChecks(floor=("make guards",), final=("make ci",))
    seen: dict[str, Any] = {}

    async def _declared(_session: Any, _product_id: Any) -> DeclaredChecks:
        return product_checks

    @asynccontextmanager
    async def _fake_open(**_kwargs: Any) -> Any:
        yield CheckEnvironmentStub()

    async def _fake_gate(_service: Any, **kwargs: Any) -> dict[str, Any]:
        seen["declared"] = kwargs.get("declared")
        return {"commands": [], "passed": True, "proved": True}

    monkeypatch.setattr(gate_commands, "product_declared_checks", _declared)
    monkeypatch.setattr(verify_environment, "open_run_check_environment", _fake_open)
    monkeypatch.setattr(inplace_gate, "run_inplace_gate", _fake_gate)

    await _loop_context.settle_client_attach(
        _Orch(),
        run=_Run(),
        work_step=_Step(),
        attempt=_Attempt(),
        box=_Box(),
        messages=[],
        baseline=None,
        cycle=0,
    )
    assert seen["declared"] is product_checks


# ── the deriver is shown what the product already runs ───────────────────────


def test_the_deriver_is_shown_the_products_own_checks() -> None:
    """Prod #1204 re-run round 2 ran the full suite twice: the deriver copied CI's test step
    and the product's final ran it again."""
    from backend.workflow.domain.gate_derivation import derivation_planner_messages

    system, user = derivation_planner_messages(
        manifests={}, changed_files=[], intent="x", product_checks=("make ci",)
    )
    assert "PRODUCT CHECKS" in system["content"]
    assert "make ci" in user["content"]
    _, bare = derivation_planner_messages(manifests={}, changed_files=[], intent="x")
    assert "already runs" not in bare["content"]


async def test_the_server_gate_hands_floor_and_final_to_the_deriver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def author(*_a: Any, **kwargs: Any) -> DerivedGate:
        seen.update(kwargs)
        return DerivedGate(applicable=True, commands=())

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=StubLlm([]))
        monkeypatch.setattr(svc, "_author_derived_gate", author)
        monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        await svc._run_derived_gate(
            run, NoopSandboxSession(str(tmp_path)), [], floor=("make g",), final=("make ci",)
        )
    assert seen["product_checks"] == ("make g", "make ci")


async def test_the_in_place_gate_hands_floor_and_final_to_the_deriver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.workflow.application.inplace_gate import run_inplace_gate
    from backend.workflow.domain.declared_checks import DeclaredChecks
    from tests.workflow.test_verify_unified_across_models import _GATE_JSON, _Box, _Llm, _Run
    from tests.workflow.test_verify_unified_across_models import _service as _unified

    svc = _unified(_Llm(_GATE_JSON))
    seen: dict[str, Any] = {}
    real = svc._author_derived_gate

    async def author(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(svc, "_author_derived_gate", author)
    await run_inplace_gate(
        svc,
        run=_Run(),
        box=_Box(manifests={"pyproject.toml": "x"}),
        declared=DeclaredChecks(floor=("make g",), final=("make ci",)),
    )
    assert seen["product_checks"] == ("make g", "make ci")


async def test_verify_tells_the_deriver_about_the_products_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _verify_product_run(
        tmp_path, monkeypatch, {FLOOR_KEY: ["true"], FINAL_KEY: ["make ci"]}, derived=("false",)
    )
    assert _AUTHOR_KWARGS["product_checks"] == ("true", "make ci")
