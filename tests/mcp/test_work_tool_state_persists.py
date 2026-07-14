"""The registry's per-run state survives across MCP calls (T2b-2).

Found by probing the live MCP surface, not by reading the code:

    file_write            → refused ("declare_verification first")   ✅ correct
    declare_verification  → "verification contract recorded"          ✅
    file_write            → refused AGAIN                             ❌

The in-process loop holds ONE :class:`ToolRegistry` for the whole run, so the latch that
``declare_verification`` sets stays set. The MCP transport builds a registry **per request**,
so every latch — the verification contract, and the ``file_read``-before-``file_edit``
grounding — is thrown away between calls. An agent on the executor could never write a file:
it would declare its contract and then be told, forever, to declare its contract.

So the state belongs to the RUN, not to the object: it is exported after each call and
restored before the next. The loop is unaffected — its registry simply never has anything to
restore.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.workflow.infrastructure.tools import ToolRegistry

pytestmark = pytest.mark.asyncio

_CONTRACT = {"checks": [{"kind": "shell", "command": "pytest -q"}]}


async def test_a_fresh_registry_refuses_to_write(tmp_path: Path) -> None:
    """The gate itself — verify-first. This is the behaviour we must not lose."""
    from backend.workflow.infrastructure.tools import ToolError

    registry = ToolRegistry(workspace_dir=tmp_path)

    with pytest.raises(ToolError):
        await registry.invoke("file_write", {"path": "a.py", "content": "x = 1"})


async def test_the_declared_contract_survives_a_rebuild(tmp_path: Path) -> None:
    """Declare on one MCP call; write on the next. Same run, different registry object."""
    first = ToolRegistry(workspace_dir=tmp_path)
    await first.invoke("declare_verification", _CONTRACT)
    state = first.export_state()

    second = ToolRegistry(workspace_dir=tmp_path)
    second.restore_state(state)
    await second.invoke("file_write", {"path": "a.py", "content": "x = 1"})

    assert (tmp_path / "a.py").read_text() == "x = 1"


async def test_grounding_survives_a_rebuild(tmp_path: Path) -> None:
    """``file_edit`` requires the agent to have READ the file first — so it edits real content
    instead of a hallucinated recall. Across MCP calls that read happened in a previous
    registry, so the grounding has to travel too."""
    (tmp_path / "a.py").write_text("x = 1")

    first = ToolRegistry(workspace_dir=tmp_path)
    await first.invoke("declare_verification", _CONTRACT)
    await first.invoke("file_read", {"path": "a.py"})
    state = first.export_state()

    second = ToolRegistry(workspace_dir=tmp_path)
    second.restore_state(state)
    await second.invoke("file_edit", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"})

    assert (tmp_path / "a.py").read_text() == "x = 2"


async def test_state_of_a_fresh_registry_is_empty(tmp_path: Path) -> None:
    """Nothing declared, nothing read — restoring it must not unlock anything."""
    from backend.workflow.infrastructure.tools import ToolError

    state = ToolRegistry(workspace_dir=tmp_path).export_state()

    registry = ToolRegistry(workspace_dir=tmp_path)
    registry.restore_state(state)

    with pytest.raises(ToolError):
        await registry.invoke("file_write", {"path": "a.py", "content": "x"})


async def test_state_is_json_round_trippable(tmp_path: Path) -> None:
    """It rides on the run's JSON payload, so it has to survive a serialize/deserialize."""
    import json

    registry = ToolRegistry(workspace_dir=tmp_path)
    await registry.invoke("declare_verification", _CONTRACT)
    await registry.invoke("file_read", {"path": "a.py"}) if (tmp_path / "a.py").exists() else None

    state = json.loads(json.dumps(registry.export_state()))

    rebuilt = ToolRegistry(workspace_dir=tmp_path)
    rebuilt.restore_state(state)
    assert rebuilt.declared_contract is not None


# ── What the agent DID has to cross the process boundary too (T3 prerequisite) ──
# The contract and the grounding latches cross it (above). The agent's WRITES do not — and
# the loop needs them. Measured on the live surface (run 96dd7cfc, 2026-07-14): the agent
# wrote clamp.py + test_clamp.py through the MCP work tools, and the verified Deliverable came
# out with ``artifact_refs: []``.
#
# That list is load-bearing: it is the changed-file list in the PR/Slack body, the settle
# knowledge tags, the design→impl handoff seed, and — in the proof/diff view — the SECURITY
# WHITELIST (a file not in artifact_refs is refused). It is also what the deliverable summary
# is composed from, which is why an executor run's summary degraded into the model's raw
# narration.
#
# The native loop gets these paths by sniffing its own in-band tool calls
# (``_invoke_tool_safely``). The MCP transport invokes the registry directly, in ANOTHER
# PROCESS, so the loop never sees them. The registry's exported state is the only channel.


async def test_a_write_is_recorded_on_the_registry(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_dir=tmp_path)
    registry.declared_contract = _CONTRACT

    await registry.invoke("file_write", {"path": "pkg/a.py", "content": "x = 1"})

    assert "pkg/a.py" in registry.written_paths


async def test_the_written_paths_survive_a_rebuild(tmp_path: Path) -> None:
    """Write on one MCP call; the loop must still learn about it after the run."""
    first = ToolRegistry(workspace_dir=tmp_path)
    first.declared_contract = _CONTRACT
    await first.invoke("file_write", {"path": "pkg/a.py", "content": "x = 1"})

    second = ToolRegistry(workspace_dir=tmp_path)
    second.restore_state(first.export_state())

    assert "pkg/a.py" in second.written_paths


async def test_an_edit_is_recorded_too(tmp_path: Path) -> None:
    registry = ToolRegistry(workspace_dir=tmp_path)
    registry.declared_contract = _CONTRACT
    await registry.invoke("file_write", {"path": "a.py", "content": "x = 1"})

    await registry.invoke(
        "file_edit", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}
    )

    assert registry.written_paths.count("a.py") == 1, "a path is recorded once, in order"


async def test_a_read_is_NOT_a_write(tmp_path: Path) -> None:
    """``grounded_paths`` is polluted with reads — it cannot stand in for the write list."""
    (tmp_path / "seen.py").write_text("x = 1")
    registry = ToolRegistry(workspace_dir=tmp_path)

    await registry.invoke("file_read", {"path": "seen.py"})

    assert registry.written_paths == []


async def test_the_declared_knowledge_survives_a_rebuild(tmp_path: Path) -> None:
    """The agent's retrospective knowledge was dropped by export_state entirely, so an
    executor-driven run could never produce a knowledge note."""
    from backend.knowledge.extraction.worth_remembering import RememberableKnowledge

    first = ToolRegistry(workspace_dir=tmp_path)
    first.declared_knowledge = RememberableKnowledge(topic="t", insight="i")

    second = ToolRegistry(workspace_dir=tmp_path)
    second.restore_state(first.export_state())

    assert second.declared_knowledge is not None
    assert second.declared_knowledge.topic == "t"
    assert second.declared_knowledge.insight == "i"
