"""Every REST run/deliverable route must have an MCP tool — asserted structurally.

Two surfaces reach the same domain (the founder's browser via REST, the agent
and the editor via MCP). A mirrored surface drifts in the direction of least
testing: someone adds ``POST /runs/{id}/retry`` for the PWA, nobody adds
``bsvibe_runs_retry``, and the agent silently cannot re-open a failed run. That
is exactly how the gaps this module was written for came to exist — six of them,
found by running this derivation for the first time (the standing audit had
counted four).

So the proposition is written as a **structure**, not as a list of tool names:

    every route the REST app mounts under /runs and /deliverables
    has a registered MCP tool derived from its path by one rule.

Enumerating names instead would pass forever after the next route is added —
the same failure mode the guard exists to stop (cf. the ``export`` merge guard
in ``test_work_state_carries_every_field``).

``_KNOWN_GAPS`` is fail-closed in *both* directions: a route that is missing
its tool and is NOT pinned fails as a new gap, and a pinned entry whose tool
now exists (or whose route is gone) fails as stale. It can only shrink.
"""

from __future__ import annotations

import re

from backend.api.v1 import deliverables as deliverables_api
from backend.api.v1.runs import router as runs_router
from backend.mcp.api import ToolRegistry
from backend.mcp.tools import register_all_tools

# Routes that legitimately have no MCP twin, each with the reason it is not a
# gap. Anything not listed here MUST have a tool. Kept as data (not a regex or
# a skip) so closing a gap forces the entry out — a stale pin fails the test.
_KNOWN_GAPS: dict[str, str] = {
    # Serves raw artifact BYTES (with a binary sniff + a traversal guard) for
    # the browser's file viewer. The MCP surface reads run files through the
    # run-scoped ``bsvibe_work_file_read`` instead; a workspace-token tool that
    # streams arbitrary bytes out of a deliverable is a separate design
    # question, not a mechanical mirror. Tracked for the parity follow-up.
    "GET /deliverables/{deliverable_id}/artifacts/{ref:path}": (
        "raw-bytes viewer surface; MCP byte reads go through bsvibe_work_file_read"
    ),
    # These two are real gaps whose fix is NOT "add a tool". Their rules live in
    # ``backend.api.v1.deliverables`` today, and the import contract "MCP context
    # depends only on Identity + Workflow + Knowledge + common" forbids
    # ``backend.mcp -> backend.api`` — deliberately, so an MCP tool never reaches
    # across a boundary the REST surface doesn't. Copying the rule into the MCP
    # module would satisfy this guard while creating exactly the drift it exists
    # to prevent, so the honest state is a pin.
    #
    # Closing them means moving the rule into ``backend.workflow.application``
    # and injecting what it may not import from the composition root — the
    # pattern ``register_all_tools`` already uses for ``record_deliverable``:
    #   * retract dispatches plugin compensation → reaches backend.extensions
    #   * report builds the narrative → reaches backend.connectors transitively
    "GET /deliverables/{deliverable_id}/report": (
        "report builder reaches backend.connectors via the narrative; same move needed"
    ),
}

# Routers are mounted by ``backend.api.v1.__init__`` under these prefixes; the
# prefix also names the MCP tool family (``/runs`` → ``bsvibe_runs_*``).
_SURFACES = (("runs", runs_router), ("deliverables", deliverables_api.router))

_PATH_PARAM = re.compile(r"^\{[^}]+\}$")


def _tool_name_for(family: str, path: str) -> str:
    """Derive the MCP tool name one route should have.

    ``""`` → ``list`` · ``/{id}`` → ``show`` · ``/{id}/<verb>`` → ``<verb>``.
    The rule is mechanical on purpose: a new route gets a predicted name
    without anyone updating this file.
    """
    segments = [s for s in path.split("/") if s]
    literals = [s for s in segments if not _PATH_PARAM.match(s)]
    if not literals:
        suffix = "show" if segments else "list"
    else:
        # ``{ref:path}`` converters keep their converter suffix in the raw path.
        suffix = literals[-1].split(":")[0]
    return f"bsvibe_{family}_{suffix}"


def _rest_routes() -> list[tuple[str, str, str]]:
    """``(key, family, tool_name)`` for every mounted run/deliverable route."""
    out: list[tuple[str, str, str]] = []
    for family, router in _SURFACES:
        for route in router.routes:
            methods = sorted(
                m for m in getattr(route, "methods", set()) if m not in {"HEAD", "OPTIONS"}
            )
            for method in methods:
                key = f"{method} /{family}{route.path}"
                out.append((key, family, _tool_name_for(family, route.path)))
    return out


def _registered_names() -> set[str]:
    registry = ToolRegistry()
    register_all_tools(registry)
    return set(registry.names())


def test_every_rest_run_and_deliverable_route_has_an_mcp_tool() -> None:
    """The proposition: no REST route on these two surfaces lacks an MCP twin."""
    names = _registered_names()
    missing = {
        key: tool
        for key, _family, tool in _rest_routes()
        if tool not in names and key not in _KNOWN_GAPS
    }
    assert not missing, (
        "REST routes with no MCP tool (add the tool, or pin it in _KNOWN_GAPS "
        f"with the reason it is not a gap): {missing}"
    )


def test_the_known_gap_pins_are_not_stale() -> None:
    """A pin that no longer describes reality must fail, so the list only shrinks."""
    names = _registered_names()
    routes = {key: tool for key, _family, tool in _rest_routes()}

    vanished = sorted(set(_KNOWN_GAPS) - set(routes))
    assert not vanished, f"_KNOWN_GAPS names routes that no longer exist: {vanished}"

    closed = sorted(key for key in _KNOWN_GAPS if routes[key] in names)
    assert not closed, f"_KNOWN_GAPS pins routes whose MCP tool now exists — remove them: {closed}"


def test_the_derivation_rule_matches_the_tools_that_already_exist() -> None:
    """Positive control: the rule must reproduce names nobody disputes.

    Without this, a derivation bug ("bsvibe_runs_" for everything) would make
    the guard above vacuously green — it would look for tools that can never
    exist and find them all pinned, or worse, find one name for every route.
    """
    assert _tool_name_for("runs", "") == "bsvibe_runs_list"
    assert _tool_name_for("runs", "/{run_id}") == "bsvibe_runs_show"
    assert _tool_name_for("runs", "/{run_id}/cancel") == "bsvibe_runs_cancel"
    assert _tool_name_for("deliverables", "/{deliverable_id}/diff") == "bsvibe_deliverables_diff"
    assert (
        _tool_name_for("deliverables", "/{deliverable_id}/artifacts/{ref:path}")
        == "bsvibe_deliverables_artifacts"
    )
    # And those three really are registered today — so a green run of the main
    # guard means "tools found", not "rule produced nothing".
    names = _registered_names()
    for existing in ("bsvibe_runs_list", "bsvibe_runs_show", "bsvibe_runs_cancel"):
        assert existing in names
