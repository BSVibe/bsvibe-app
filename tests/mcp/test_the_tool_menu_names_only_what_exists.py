"""#1116 — what the MCP tool menu says must be true.

Tool descriptions are not comments: every agent and MCP client reads them as the
menu of what BSVibe can do. ``bsvibe_run_routing_rules_list`` said *"e.g. design →
executor/codex … Distinct from bsvibe_routing_rules_*"* — but codex cannot take
BSVibe's tools (``dispatch.adapter``: agentic work routed to it is refused) and
the ``bsvibe_routing_rules_*`` tools were removed (migration
``20260711_drop_layer2_routing_rules``). A menu that recommends a dead executor
and names tools that do not exist sends its reader somewhere that fails.
"""

from __future__ import annotations

import re
import uuid

import pytest

from backend.dispatch.adapter import supports_remote_tools
from backend.mcp.server import build_registry
from backend.workflow.domain.client_worktree import worktree_branch
from plugin.github.webhook import _is_own_branch

_TOOL_NAME = re.compile(r"\bbsvibe_[a-z0-9_]*[a-z0-9](?:_\*)?")
_EXECUTOR_TARGET = re.compile(r"\bexecutor/([a-z_]+)")


@pytest.fixture(scope="module")
def menu() -> dict[str, str]:
    registry = build_registry()
    return {name: registry.get(name).description for name in registry.names()}  # type: ignore[union-attr]


def test_every_tool_a_description_names_exists(menu: dict[str, str]) -> None:
    names = set(menu)
    phantom: list[tuple[str, str]] = []
    for tool, description in menu.items():
        for mention in _TOOL_NAME.findall(description):
            if mention.endswith("*"):
                prefix = mention[:-1]
                if not any(n.startswith(prefix) for n in names):
                    phantom.append((tool, mention))
            elif mention not in names:
                phantom.append((tool, mention))
    assert phantom == []


def test_no_description_routes_work_to_an_executor_that_cannot_take_our_tools(
    menu: dict[str, str],
) -> None:
    dead = [
        (tool, executor)
        for tool, description in menu.items()
        for executor in _EXECUTOR_TARGET.findall(description)
        if not supports_remote_tools(executor)
    ]
    assert dead == []


# ---------------------------------------------------------------------------
# Three branch rules — the self-PR skip must know every one BSVibe opens PRs from
# ---------------------------------------------------------------------------


def _pr_from(head: str) -> dict:
    return {"pull_request": {"head": {"ref": head}}}


def test_a_client_attach_run_branch_is_recognised_as_our_own() -> None:
    """A client_attach run pushes ``run/<8hex>`` from the founder's machine and the
    server opens the PR from it (``_github_in_place``). With the founder's token
    GitHub names a USER as the sender, so the branch is the only marker — missing
    it makes BSVibe's own PR a fresh run of BSVibe."""
    assert _is_own_branch(_pr_from(worktree_branch(uuid.uuid4())))


@pytest.mark.parametrize("head", ["run/fix-login", "run/1234", "feature/run/abcdef12"])
def test_a_human_branch_that_merely_starts_with_run_is_not(head: str) -> None:
    assert not _is_own_branch(_pr_from(head))
