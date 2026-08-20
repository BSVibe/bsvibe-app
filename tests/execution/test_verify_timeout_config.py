"""Verify-phase command timeouts are CONFIGURABLE, not hardcoded.

A derived-gate check (or a declared command check) that legitimately runs the
repo's test suite takes minutes — the old ``VERIFY_TIMEOUT_S = 60`` /
``GATE_CMD_TIMEOUT_S = 300`` truncated a real gate. The per-command verify
timeout now comes from ``settings.verify_command_timeout_s`` and the derived
gate command timeout from ``settings.verify_gate_command_timeout_s``.

These drive the REAL ``VerificationService`` methods with a recording box that
captures the ``timeout_s`` each command runs with.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from backend.config import get_settings
from backend.workflow.application.verification_service import VerificationService
from backend.workflow.domain.gate_derivation import DerivedCommand, DerivedGate
from backend.workflow.domain.verifier_contract import VerificationCheck, VerificationContract
from backend.workflow.infrastructure.sandbox.protocol import SandboxResult
from tests._support import memory_session


class _NoLlm:
    async def complete(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> Any:  # pragma: no cover - unused in these methods
        raise AssertionError("llm should not be called in these tests")


class _TimeoutRecordingBox:
    """Scripted SandboxSession recording each ``(command, timeout_s)`` pair."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    @property
    def workspace_mount(self) -> str:
        return "/workspace"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.calls.append((command, timeout_s))
        return SandboxResult(exit_code=0, stdout="ok", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        return b""

    async def write_file(self, rel_path: str, content: bytes) -> None:  # pragma: no cover
        return None

    async def list_dir(self, rel_path: str) -> list[str]:  # pragma: no cover
        return []


async def test_command_check_uses_configured_verify_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "verify_command_timeout_s", 4242.0)
    box = _TimeoutRecordingBox()
    async with memory_session() as session:
        svc = VerificationService(session=session, llm=_NoLlm())
        contract = VerificationContract(
            checks=(VerificationCheck(kind="command", command="uv run pytest -q"),)
        )
        await svc._run_command_checks(contract, box)

    # The uv-sync prep runs with VENV_SYNC_TIMEOUT_S; the actual check is the
    # call whose command ends with the declared command.
    check_timeouts = [t for (c, t) in box.calls if c.endswith("uv run pytest -q")]
    assert check_timeouts == [4242.0]


async def test_derived_gate_uses_configured_gate_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "verify_gate_command_timeout_s", 9191.0)
    box = _TimeoutRecordingBox()
    run = SimpleNamespace(product_id=uuid.uuid4(), payload={"intent_text": "do the thing"})

    async def _fake_author(
        intent: str,
        manifests: dict[str, str],
        written_paths: list[str],
        baseline: str | None = None,
    ) -> DerivedGate:
        return DerivedGate(
            applicable=True,
            commands=(DerivedCommand(command="uv run pytest"),),
        )

    async with memory_session() as session:
        svc = VerificationService(session=session, llm=_NoLlm())
        monkeypatch.setattr(svc, "_is_real_worktree", lambda r: True)
        monkeypatch.setattr(svc, "_author_derived_gate", _fake_author)
        await svc._run_derived_gate(run, box, ["backend/x.py"])

    gate_timeouts = [t for (c, t) in box.calls if c.endswith("uv run pytest")]
    assert gate_timeouts == [9191.0]
