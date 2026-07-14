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
