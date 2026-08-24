"""The two execution models must run the SAME verification, not two of them.

client_attach and server_sandbox differ in WHERE commands run — that is what
``SandboxSession`` abstracts. They should not differ in WHAT verification means.

Until now they did: ``declare_verification`` sat on the WORKSPACE axis on the
premise that "declaring a contract presumes a server-side worktree to run it
against". That premise is false, and the in-place gate is its own counter-
example — it runs the repo's own commands on the founder's machine through the
very same box. Declaring a contract is a statement recorded on the RUN; where
its commands later execute is a separate question, already answered.

So a client_attach run got a strictly weaker guarantee for no reason other than
where the founder keeps their source.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.infrastructure.sandbox import SandboxResult

pytestmark = pytest.mark.asyncio


def test_declare_verification_is_offered_to_every_execution_model() -> None:
    """It is a declaration to BSVibe, not a mutation of the working tree.

    There is one surface now, so this holds by construction rather than by an
    axis rule — assert it anyway: it is the property that mattered."""
    from backend.workflow.application.tool_registry import WORK_TOOL_MCP_NAMES

    assert "bsvibe_work_declare_verification" in WORK_TOOL_MCP_NAMES


class _Box:
    runs_in_place = True
    provisions_venv = False

    def __init__(self, *, manifests: dict[str, str], exits: dict[str, int] | None = None) -> None:
        self._manifests = manifests
        self._exits = exits or {}
        self.execs: list[str] = []

    @property
    def workspace_mount(self) -> str:
        return "/Users/founder/proj"

    async def exec(self, command: str, *, timeout_s: float, shell: bool = False) -> SandboxResult:
        self.execs.append(command)
        code = next((c for k, c in self._exits.items() if k in command), 0)
        return SandboxResult(exit_code=code, stdout="out", stderr="", timed_out=False)

    async def read_file(self, rel_path: str, max_bytes: int) -> bytes:
        from backend.workflow.infrastructure.sandbox import SandboxError

        if rel_path not in self._manifests:
            raise SandboxError(f"missing {rel_path}")
        return self._manifests[rel_path].encode()

    async def write_file(self, rel_path: str, content: bytes) -> None:
        raise AssertionError("the server never writes to the founder's tree")

    async def list_dir(self, rel_path: str) -> list[str]:
        return ["pyproject.toml"]


class _Llm:
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, *, messages: Any, tools: Any = None) -> Any:
        return type("_Turn", (), {"content": self._content})()


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None: ...
    async def flush(self) -> None: ...


class _Run:
    def __init__(self, declared: Any = None) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {"intent_text": "fix a bug"}
        self.declared_contract = declared


_GATE_JSON = (
    '{"applicable": true, "commands": ['
    '{"kind": "test", "command": "uv run pytest -q", "rationale": "repo tests"}]}'
)


def _service(llm: Any) -> Any:
    from backend.workflow.application.verification_service import VerificationService

    return VerificationService(session=_FakeSession(), llm=llm, retriever=None)


async def test_declared_contract_commands_run_in_place() -> None:
    """Declaring is worthless if nothing executes it. A tool that is advertised
    but whose effect is never wired is the exact drift this codebase keeps
    getting bitten by."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "[project]\nname='x'\n"})
    blob = await run_inplace_gate(
        _service(_Llm(_GATE_JSON)),
        run=_Run(declared={"checks": [{"kind": "command", "command": "make verify"}]}),
        box=box,
    )

    assert blob is not None
    assert any("make verify" in c for c in box.execs), box.execs
    assert any(r.get("command") == "make verify" for r in blob["commands"]), blob["commands"]


async def test_a_failing_declared_check_is_an_honest_failure() -> None:
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "x"}, exits={"make verify": 1})
    blob = await run_inplace_gate(
        _service(_Llm(_GATE_JSON)),
        run=_Run(declared={"checks": [{"kind": "command", "command": "make verify"}]}),
        box=box,
    )

    assert blob is not None
    assert blob["passed"] is False
    assert blob["proved"] is False


async def test_no_declared_contract_still_runs_the_derived_gate() -> None:
    """Unification must not make the derived gate conditional on a declaration —
    it is the half that works when the agent declares nothing."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "x"})
    blob = await run_inplace_gate(_service(_Llm(_GATE_JSON)), run=_Run(), box=box)

    assert blob is not None
    assert blob["proved"] is True
    assert any("pytest" in c for c in box.execs)
