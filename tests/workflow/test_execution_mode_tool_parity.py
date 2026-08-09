"""The two execution models must not drift on what BSVibe offers the agent.

A ``client_attach`` run acts with the CLI's NATIVE tools on the founder's own
tree, so the WORKSPACE half of the surface (file / shell) is supplied by the CLI
rather than by BSVibe. That choice is correct. What is not correct — and what
this module pins — is that the same switch also withheld the PLATFORM half
(knowledge, asking the founder, emitting a deliverable), which has nothing to do
with where the source lives. It is served by the server either way.

Consequence, measured 2026-08-09: a client_attach run cannot call
``emit_deliverable`` → no Deliverable row → ``connector_dispatch`` has nothing
to load → NOTHING is ever delivered out. BStockReport M5 (weekly report →
telegram) is structurally impossible in that mode, not merely unimplemented.

So the axes are split and BOTH derive from one source: a new platform tool is
offered to every execution model or to none.
"""

from __future__ import annotations

import pytest


def test_workspace_and_platform_axes_partition_the_surface() -> None:
    """Every MCP work tool belongs to exactly one axis — no tool falls through."""
    from backend.workflow.application.tool_registry import (
        PLATFORM_TOOL_MCP_NAMES,
        WORK_TOOL_MCP_NAMES,
        WORKSPACE_TOOL_MCP_NAMES,
    )

    assert set(WORKSPACE_TOOL_MCP_NAMES) | set(PLATFORM_TOOL_MCP_NAMES) == set(WORK_TOOL_MCP_NAMES)
    assert not set(WORKSPACE_TOOL_MCP_NAMES) & set(PLATFORM_TOOL_MCP_NAMES)


def test_platform_axis_is_what_the_server_offers_regardless_of_where_source_lives() -> None:
    from backend.workflow.application.tool_registry import PLATFORM_TOOL_MCP_NAMES

    assert set(PLATFORM_TOOL_MCP_NAMES) == {
        "bsvibe_work_knowledge_search",
        "bsvibe_work_ask_user_question",
        "bsvibe_work_emit_deliverable",
    }


def test_workspace_axis_is_what_touching_the_working_tree_needs() -> None:
    """``declare_verification`` sits here on purpose: declaring a contract
    presumes a server-side worktree to run it against. client_attach has a
    STRONGER replacement — the in-place derived gate (#705), whose verdict is a
    real exit code on the founder's machine."""
    from backend.workflow.application.tool_registry import WORKSPACE_TOOL_MCP_NAMES

    assert set(WORKSPACE_TOOL_MCP_NAMES) == {
        "bsvibe_work_file_read",
        "bsvibe_work_file_list",
        "bsvibe_work_file_write",
        "bsvibe_work_file_edit",
        "bsvibe_work_shell_exec",
        "bsvibe_work_declare_verification",
    }


@pytest.mark.parametrize(
    ("execution_target", "expected"),
    [
        ("server_sandbox", "all"),
        ("client_attach", "platform"),
        ("", "all"),  # absent → today's default, never a silent downgrade
    ],
)
def test_mcp_surface_per_execution_model(execution_target: str, expected: str) -> None:
    from backend.workflow.application.tool_registry import (
        PLATFORM_TOOL_MCP_NAMES,
        WORK_TOOL_MCP_NAMES,
        mcp_tool_names_for,
    )

    want = PLATFORM_TOOL_MCP_NAMES if expected == "platform" else WORK_TOOL_MCP_NAMES
    assert mcp_tool_names_for(execution_target) == want


def test_every_execution_model_gets_the_whole_platform_axis() -> None:
    """The anti-drift property, stated directly: add a platform tool and every
    model receives it. This is the assertion that fails if someone gives one
    model a capability the other silently lacks."""
    from backend.workflow.application.tool_registry import (
        PLATFORM_TOOL_MCP_NAMES,
        mcp_tool_names_for,
    )

    for target in ("server_sandbox", "client_attach"):
        assert set(PLATFORM_TOOL_MCP_NAMES) <= set(mcp_tool_names_for(target)), target
