"""The derived gate neither repeats the product's whole-suite run nor re-issues a timeout.

Prod #1343 (run bc7c9327, 2026-11-09): the deriver re-worded the product's own ``uv run pytest -q``
into ``CI=true uv run pytest --cov=backend … -v`` - the whole suite twice in one verification - and
that slower copy timed out three rounds running, because a timed-out command was handed back to the
deriver as "re-emit this". No code change fixes a time limit.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.workflow.application.gate_commands import _timed_out
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.gate_derivation import duplicates_product_run
from backend.workflow.infrastructure.sandbox.noop_manager import NoopSandboxSession
from tests._support import memory_session
from tests.execution.test_verification_service import StubLlm, _make_run

_FULL = "CI=true uv run pytest --cov=backend -v"
_NARROW = "uv run pytest tests/delivery -v"


class _RecordingBox(NoopSandboxSession):
    """Records what the gate ran; nothing is executed."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.ran: list[str] = []

    async def exec(self, command: str, **_kw: Any) -> Any:  # type: ignore[override]
        self.ran.append(command)
        return SimpleNamespace(exit_code=0, timed_out=False, stdout="", stderr="")


def _service(session: Any, monkeypatch: pytest.MonkeyPatch, reply: str) -> VerificationService:
    svc = VerificationService(session=session, llm=StubLlm([]))
    seen: list[list[dict[str, str]]] = []

    async def complete(_run: Any, messages: list[dict[str, str]], *_a: Any, **_k: Any) -> Any:
        seen.append(messages)
        return SimpleNamespace(content=reply)

    monkeypatch.setattr(svc, "_complete", complete)
    monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
    svc.seen = seen  # type: ignore[attr-defined]
    return svc


async def test_a_full_pytest_run_duplicating_the_product_check_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reply = json.dumps(
        {
            "applicable": True,
            "commands": [
                {"command": _FULL, "kind": "test"},
                {"command": _NARROW, "kind": "test"},
            ],
        }
    )
    async with memory_session() as session:
        svc = _service(session, monkeypatch, reply)
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        box = _RecordingBox(str(tmp_path))
        await svc._run_derived_gate(run, box, [], final=("uv run pytest -q",))
    assert not any("--cov=backend" in c for c in box.ran), box.ran
    assert any(_NARROW in c for c in box.ran), box.ran


async def test_a_timed_out_command_is_listed_apart_from_the_re_emit_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with memory_session() as session:
        svc = _service(session, monkeypatch, '{"applicable": false, "commands": []}')
        run = await _make_run(session)
        run.product_id = uuid.uuid4()
        await svc._run_derived_gate(
            run,
            _RecordingBox(str(tmp_path)),
            [],
            previous=("uv run ruff check .",),
            timed_out=(_FULL,),
        )
    user = svc.seen[0][-1]["content"]  # type: ignore[attr-defined]
    re_emit, timed_out = user.split("TIMED OUT")
    assert "re-emit" in re_emit and "uv run ruff check ." in re_emit
    assert _FULL not in re_emit
    assert _FULL in timed_out


def test_a_result_that_ended_without_an_exit_code_counts_as_timed_out() -> None:
    assert _timed_out({"command": "x", "status": "failed", "exit_code": None})
    assert _timed_out({"command": "x", "status": "failed", "exit_code": 1, "timed_out": True})
    assert not _timed_out({"command": "x", "status": "failed", "exit_code": 1})


@pytest.mark.parametrize(
    ("command", "duplicate"),
    [
        (_FULL, True),
        ("pytest", True),
        ("python -m pytest -n 4 --cov", True),
        ("uv run pytest --cov backend", True),
        ("uv run pytest --cov-report term-missing", True),
        ("uv run pytest --cov-fail-under 80", True),
        ("uv run pytest --cov -n 4", True),
        ("uv run pytest --cov --cov-report term-missing", True),
        ("uv run pytest --cov --cov-fail-under 80", True),
        ("uv run pytest --cov tests/unit", False),
        (_NARROW, False),
        ("uv run pytest -k money", False),
        ("uv run ruff check backend", False),
        ("uv run pytest && echo ok", False),
    ],
)
def test_only_a_whole_suite_pytest_run_duplicates_the_product_check(
    command: str, duplicate: bool
) -> None:
    assert (duplicates_product_run(command, ("uv run pytest -q",)) is not None) is duplicate


def test_a_narrowed_product_check_does_not_make_the_full_run_a_duplicate() -> None:
    assert duplicates_product_run(_FULL, ("uv run pytest tests/unit -q",)) is None
