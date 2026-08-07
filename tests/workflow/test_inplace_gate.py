"""#692 in-place verify B/2c — the client_attach run's gate actually RUNS.

Today a client_attach run ends at ``review_ready`` + ``proof_state=UNTESTED``:
the server holds no source, so no gate could run and claiming otherwise would be
false. The honesty of the gate, though, rests on "the command's EXIT CODE is the
verdict, never a model's opinion" — a property that survives moving the command
to the founder's machine. So: run the repo's OWN derived gate there, and let the
exit codes decide.

Why a dedicated path rather than falling through to ``verify``: a client_attach
agent works with the CLI's NATIVE tools, so BSVibe's MCP work tools (and with
them ``declare_verification``) are withheld. ``_assemble_contract`` therefore
always returns ``None`` and the normal path would end in a
``no_verification_declared`` decision — strictly WORSE than today's terminal.
The derived gate needs no declared contract: it reads the repo's own manifests
and derives the commands.

The ladder this pins:
* no manifests on the founder's machine → no gate exists → UNTESTED (unchanged).
* deriver could not run → UNTESTED (never a silent PROVED).
* gate ran, a command FAILED → honest failure fed back to the agent.
* gate ran and passed with at least one command actually executed → PROVED.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.workflow.infrastructure.sandbox import SandboxResult

pytestmark = pytest.mark.asyncio


class _Box:
    """In-place box stand-in: declares it runs on the founder's machine."""

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
        return []


class _Llm:
    """Stands in for the gate deriver."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, *, messages: Any, tools: Any = None) -> Any:
        self.calls += 1
        return type("_Turn", (), {"content": self._content})()


_GATE_JSON = (
    '{"applicable": true, "commands": ['
    '{"kind": "test", "command": "uv run pytest -q", "rationale": "repo tests"}]}'
)


def _service(llm: Any) -> Any:
    from backend.workflow.application.verification_service import VerificationService

    return VerificationService(session=_FakeSession(), llm=llm, retriever=None)


class _FakeSession:
    """Records adds; commit/flush are no-ops (no DB in this tier)."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


class _Run:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.payload: dict[str, Any] = {"intent_text": "add a feature"}


async def test_gate_runs_on_the_founder_machine_and_proves_on_zero_exit() -> None:
    """The derived command RUNS in the founder's workspace and its exit code is
    the verdict — the same honesty the server-sandbox gate rests on."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "[project]\nname='x'\n"})
    llm = _Llm(_GATE_JSON)
    blob = await run_inplace_gate(_service(llm), run=_Run(), box=box)

    assert blob is not None
    assert blob["passed"] is True
    assert blob["proved"] is True, "a command actually ran and passed → PROVED is honest"
    assert any("pytest" in c for c in box.execs), box.execs


async def test_failing_command_is_an_honest_failure_not_a_pass() -> None:
    """A non-zero exit is a REAL gate failure — never rounded up to PROVED."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "x"}, exits={"pytest": 1})
    blob = await run_inplace_gate(_service(_Llm(_GATE_JSON)), run=_Run(), box=box)

    assert blob is not None
    assert blob["passed"] is False
    assert blob["proved"] is False


async def test_missing_tool_is_unavailable_not_a_false_failure() -> None:
    """exit 127 = the tool isn't on that machine. Recorded, but it neither fails
    the gate nor proves anything (matching the server gate's semantics)."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "x"}, exits={"pytest": 127})
    blob = await run_inplace_gate(_service(_Llm(_GATE_JSON)), run=_Run(), box=box)

    assert blob is not None
    assert blob["passed"] is True, "unavailable is not a failure"
    assert blob["proved"] is False, "nothing actually ran → nothing is proven"


async def test_no_manifest_means_no_gate_exists() -> None:
    """A repo with no toolchain declaration is legitimately gateless: return None
    so the run keeps today's UNTESTED terminal. The deriver is not even called."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    llm = _Llm(_GATE_JSON)
    blob = await run_inplace_gate(_service(llm), run=_Run(), box=_Box(manifests={}))

    assert blob is None
    assert llm.calls == 0


async def test_deriver_failure_never_becomes_a_silent_proof() -> None:
    """The deriver could not produce a gate → NOT proven (fail-closed). The run
    falls back to founder review rather than claiming a proof it does not have."""
    from backend.workflow.application.inplace_gate import run_inplace_gate

    box = _Box(manifests={"pyproject.toml": "x"})
    blob = await run_inplace_gate(_service(_Llm("not json at all")), run=_Run(), box=box)

    assert blob is not None
    assert blob["proved"] is False
    assert blob["passed"] is False, "a manifest exists but no gate ran → fail CLOSED"
    assert box.execs == [], "nothing should have been dispatched"


async def test_gate_result_is_persisted_so_proved_has_visible_evidence() -> None:
    """PROVED must be inspectable: a VerificationResult row carries the derived
    gate blob (what ran, exit codes) onto the proof surface."""
    from backend.workflow.application.inplace_gate import run_inplace_gate
    from backend.workflow.infrastructure.db import VerificationResult

    service = _service(_Llm(_GATE_JSON))
    await run_inplace_gate(service, run=_Run(), box=_Box(manifests={"pyproject.toml": "x"}))

    rows = [o for o in service._session.added if isinstance(o, VerificationResult)]
    assert len(rows) == 1
    assert rows[0].result["derived_gate"]["commands"][0]["status"] == "passed"


# ── the terminal: proof_state must follow the gate, never the hope ───────────


class _Step:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status: Any = None
        self.proof_state: Any = None


class _Attempt:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.phase: Any = None
        self.finished_at: Any = None


@pytest.mark.parametrize(
    ("gate", "expected_proved"),
    [
        ({"passed": True, "proved": True}, True),
        ({"passed": True, "proved": False}, False),  # gateless / all unavailable
        (None, False),  # no gate could run at all
    ],
)
async def test_terminal_proof_state_follows_the_gate(
    gate: dict[str, Any] | None, expected_proved: bool
) -> None:
    from backend.workflow.application._loop_context import client_attach_terminal
    from backend.workflow.infrastructure.db import ProofState

    step = _Step()
    client_attach_terminal(_Run(), step, _Attempt(), gate=gate)
    if expected_proved:
        assert step.proof_state is ProofState.PROVED
    else:
        assert step.proof_state is not ProofState.PROVED
