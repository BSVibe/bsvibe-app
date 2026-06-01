"""Lift 0c — assert the static DangerAnalyzer (load-time AST + LLM scan) is GONE.

This file is the delta-asserting RED-first proof for Lift 0c (YAGNI rollback
of the static plugin-load-time DangerAnalyzer). v8 §13 / D7 lists
``backend/plugins/analyzer.py`` for full deletion under Lift 0; Lift 0c is
the third + FINAL Lift-0 PR (after #224's per-call evaluator rollback and
#225's auto-compensation rollback) that retires the YAGNI safety surface.

The deltas attributable to this lift:

1. **Module gone.** ``backend.extensions.plugin.analyzer`` no longer importable —
   ``DangerAnalyzer`` / ``StaticAnalyzer`` symbols are removed, and the
   package no longer re-exports them.

2. **Plugin load does not invoke a static danger scan.** Loading a plugin
   directory via :class:`backend.extensions.plugin.loader.PluginLoader` does NOT
   read the plugin source for AST scan nor await any LLM scan. The
   loader's constructor no longer accepts a ``danger_analyzer`` arg, and
   no ``danger_map`` attribute remains. Real PG when ``BSVIBE_DATABASE_URL``
   is set, in-memory SQLite otherwise — though this delta has no DB
   surface (loader is a pure-disk component), the harness substrate matches
   the Lift 0b style so the file lands cleanly alongside.

3. **``is_dangerous`` flag fate consistent — option (a) chosen.** No
   ``is_dangerous`` field remains on :class:`ConnectorActionTool` (its
   only source was the static analyzer; the founder confirms there is no
   manual ``@p.action(dangerous=True)`` opt-in to preserve). The
   :class:`ConnectorActionResolver` constructor no longer takes a
   ``danger_map``.

4. **Backward-compat — ``@p.action`` surface intact.** The agent loop's
   connector-action tool surface remains: ``github.list_issues``,
   ``sentry.list_issues``, the existing connector actions, plus the
   workspace built-ins (``invoke_skill``, ``knowledge_search``,
   ``emit_deliverable``) still appear in the agent tool registry and
   dispatch through the unchanged :class:`PluginRunner`.

5. **No dead-code Safe Mode gate.** The pre-M2 ``tool.is_dangerous and
   safe_mode`` gate that Lift 0a restored is removed in 0c: with no
   producer of ``is_dangerous`` left, the gate is dead code and the
   ``connector_action_approval`` Decision can never be created. We assert
   the source no longer references ``is_dangerous`` and that a
   ``WorkspaceRow(safe_mode=True)`` workspace dispatches a connector
   action directly with no Decision row.

DB column status: there is no Alembic migration adding ``is_dangerous`` to
any plugin/action table — the flag was purely an in-memory load-time
verdict. No migration / column work is needed.
"""

from __future__ import annotations

import importlib
import inspect
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.extensions.plugin.loader import PluginLoader
from backend.identity.workspaces_db import WorkspaceRow
from backend.workflow.application.agent_loop import RunOrchestrator
from backend.workflow.infrastructure.connector_actions import (
    ConnectorActionResolver,
    ConnectorActionTool,
)
from backend.workflow.infrastructure.db import Decision
from backend.workflow.infrastructure.sandbox import NoopSandboxManager
from tests._support import memory_session

# Helpers from the orchestrator's own test module — these are the documented
# test seams (ScriptedLlm, _fake_action_plugin, _fake_account, _tc,
# _declare_command, _make_run) used across the connector action test suite.
# Importing them keeps this glue test aligned with the rest of the
# orchestrator harness instead of re-inventing a parallel one.
from tests.execution.test_run_orchestrator import (
    LoopTurn,
    ScriptedLlm,
    _declare_command,
    _fake_account,
    _fake_action_plugin,
    _make_run,
    _tc,
    _tool_names,
)

# ---------------------------------------------------------------------------
# Delta 1: backend.extensions.plugin.analyzer module is gone
# ---------------------------------------------------------------------------


def test_analyzer_module_deleted() -> None:
    """``backend.extensions.plugin.analyzer`` is fully removed (D7 / Lift 0)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.extensions.plugin.analyzer")


def test_plugins_init_does_not_reexport_analyzer_symbols() -> None:
    """``backend.extensions.plugin`` no longer exposes the dead surface."""
    import backend.extensions.plugin as pkg

    assert not hasattr(pkg, "DangerAnalyzer")
    assert not hasattr(pkg, "StaticAnalyzer")
    assert "DangerAnalyzer" not in pkg.__all__
    assert "StaticAnalyzer" not in pkg.__all__


# ---------------------------------------------------------------------------
# Delta 2: PluginLoader no longer invokes any static danger analysis
# ---------------------------------------------------------------------------


def test_plugin_loader_constructor_drops_danger_analyzer_arg() -> None:
    """Surface delta: ``PluginLoader.__init__`` no longer accepts
    ``danger_analyzer`` (or any analyzer hook). The keyword is gone."""
    sig = inspect.signature(PluginLoader.__init__)
    assert "danger_analyzer" not in sig.parameters, (
        "Lift 0c removes the static DangerAnalyzer injection point — "
        "PluginLoader.__init__ must not accept danger_analyzer."
    )


def test_plugin_loader_has_no_danger_map_attribute(tmp_path: Path) -> None:
    """Surface + runtime delta: a freshly built ``PluginLoader`` exposes
    no ``danger_map`` attribute, before or after load. The whole concept
    is retired."""
    loader = PluginLoader(plugins_dir=tmp_path)
    assert not hasattr(loader, "danger_map")


def test_plugin_loader_source_does_not_invoke_analyzer() -> None:
    """Source-level delta: ``loader.py`` no longer references the
    analyzer or any ``danger_*`` symbol. Catches a regression where
    someone wires a renamed hook back without re-running the substrate
    fixture."""
    import backend.extensions.plugin.loader as mod

    src = inspect.getsource(mod)
    assert "DangerAnalyzer" not in src
    assert "danger_analyzer" not in src
    assert "danger_map" not in src
    assert "from backend.extensions.plugin.analyzer" not in src


# ---------------------------------------------------------------------------
# Delta 3: is_dangerous flag fate — option (a), removed entirely
# ---------------------------------------------------------------------------


def test_connector_action_tool_has_no_is_dangerous_field() -> None:
    """``ConnectorActionTool`` is now a 3-field dataclass: plugin, action,
    account. The ``is_dangerous`` field is gone because its only producer
    (the static analyzer) is gone, and there is no manual opt-in."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ConnectorActionTool)}
    assert "is_dangerous" not in field_names, (
        "Lift 0c — is_dangerous had no producer left and no manual opt-in. "
        "Field is removed from ConnectorActionTool."
    )
    assert field_names == {"plugin", "action", "account"}


def test_connector_action_resolver_constructor_drops_danger_map() -> None:
    """``ConnectorActionResolver.__init__`` no longer takes ``danger_map``."""
    sig = inspect.signature(ConnectorActionResolver.__init__)
    assert "danger_map" not in sig.parameters, (
        "Lift 0c — the resolver no longer carries a load-time danger verdict. "
        "danger_map kwarg is removed."
    )


# ---------------------------------------------------------------------------
# Delta 4: @p.action surface intact — the agent loop still sees connector
# actions + the workspace built-ins. We pick github.list_issues + sentry.
# list_issues (the two real read-only @p.actions kept in M2) as canaries.
# ---------------------------------------------------------------------------


async def test_github_list_issues_still_in_agent_tool_schema(tmp_path: Path) -> None:
    """The github connector's ``list_issues`` @p.action still surfaces."""
    from plugin.github import plugin as github_module

    ws = uuid.uuid4()
    meta = github_module.p.meta
    assert "list_issues" in meta.actions  # static declaration still holds

    account = _fake_account(ws, "github")
    # Post-Lift-0c: no is_dangerous kwarg.
    tool = ConnectorActionTool(
        plugin=meta,
        action=meta.actions["list_issues"],
        account=account,
    )

    class _FakeProvider:
        def __init__(self, tools: list[ConnectorActionTool]) -> None:
            self._tools = tools

        async def list_actions(self, workspace_id: uuid.UUID) -> list[ConnectorActionTool]:
            return [t for t in self._tools if t.account.workspace_id == workspace_id]

        def credentials_for(self, tool: ConnectorActionTool) -> dict[str, object]:
            return {"token": "stub"}

        async def dispatch(self, tool, *, credentials, kwargs):  # type: ignore[no-untyped-def]
            return {"ok": True}

    provider = _FakeProvider([tool])
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=False))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    names = _tool_names(llm.calls[0]["tools"])
    assert "github__list_issues" in names


async def test_sentry_list_issues_still_in_agent_tool_schema(tmp_path: Path) -> None:
    """The sentry connector's ``list_issues`` @p.action still surfaces."""
    from plugin.sentry import plugin as sentry_module

    ws = uuid.uuid4()
    meta = sentry_module.p.meta
    assert "list_issues" in meta.actions

    account = _fake_account(ws, "sentry")
    tool = ConnectorActionTool(
        plugin=meta,
        action=meta.actions["list_issues"],
        account=account,
    )

    class _FakeProvider:
        def __init__(self, tools: list[ConnectorActionTool]) -> None:
            self._tools = tools

        async def list_actions(self, workspace_id: uuid.UUID) -> list[ConnectorActionTool]:
            return [t for t in self._tools if t.account.workspace_id == workspace_id]

        def credentials_for(self, tool: ConnectorActionTool) -> dict[str, object]:
            return {"token": "stub"}

        async def dispatch(self, tool, *, credentials, kwargs):  # type: ignore[no-untyped-def]
            return {"ok": True}

    provider = _FakeProvider([tool])
    llm = ScriptedLlm([LoopTurn(content="done", tool_calls=())])
    async with memory_session() as session:
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=False))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        await orch.run(run=run, workspace_dir=tmp_path)

    names = _tool_names(llm.calls[0]["tools"])
    assert "sentry__list_issues" in names


# ---------------------------------------------------------------------------
# Delta 5: No dead-code Safe Mode gate — the pre-M2 tool.is_dangerous gate
# restored in Lift 0a is now removed because its source is gone.
# ---------------------------------------------------------------------------


def test_orchestrator_source_no_longer_references_is_dangerous() -> None:
    """Source-level delta: the workflow loop modules no longer reference
    ``is_dangerous`` anywhere — the gate restored in Lift 0a is removed in 0c
    since the flag has no producer.

    Post-Lift H3c the legacy ``backend.execution.orchestrator`` shim is
    deleted; the loop now lives in :mod:`backend.workflow.application` (H2a
    decomposition: agent_loop, tool_registry, run_persistence, etc). Assert
    the call-site identifier is gone across every successor module.
    """
    import backend.workflow.application.agent_loop as agent_loop_mod
    import backend.workflow.application.run_persistence as run_persistence_mod
    import backend.workflow.application.tool_registry as tool_registry_mod
    import backend.workflow.domain.emit_deliverable as emit_deliverable_mod

    for mod in (
        agent_loop_mod,
        tool_registry_mod,
        run_persistence_mod,
        emit_deliverable_mod,
    ):
        src = inspect.getsource(mod)
        assert "is_dangerous" not in src, (
            f"Lift 0c — the tool.is_dangerous gate is dead code in {mod.__name__}."
        )
        assert "danger_map" not in src, mod.__name__
        assert "DangerAnalyzer" not in src, mod.__name__


async def test_safe_mode_workspace_dispatches_connector_action_directly(
    tmp_path: Path,
) -> None:
    """Behaviour delta: a workspace with ``safe_mode=True`` no longer
    gates a connector action — the LLM's call dispatches and no
    ``connector_action_approval`` Decision row is created.

    Pre-Lift-0c (post-0a) a ``tool.is_dangerous=True`` could have been
    set by the loader's danger_map and a safe_mode workspace would have
    routed the call to a ``connector_action_approval`` Decision. Post-0c,
    the flag has no producer left and the gate is gone, so dispatch is
    direct regardless of safe_mode.
    """
    ws = uuid.uuid4()
    plugin = _fake_action_plugin("slack", action_name="post_message")
    account = _fake_account(ws, "slack")
    # Post-0c: ConnectorActionTool has no is_dangerous field. The tool
    # below is built without it — the type itself is the assertion that
    # the dead gate is gone.
    tool = ConnectorActionTool(
        plugin=plugin, action=plugin.actions["post_message"], account=account
    )

    class _FakeProvider:
        def __init__(self, tools: list[ConnectorActionTool]) -> None:
            self._tools = tools
            self.dispatched: list[dict[str, object]] = []

        async def list_actions(self, workspace_id: uuid.UUID) -> list[ConnectorActionTool]:
            return [t for t in self._tools if t.account.workspace_id == workspace_id]

        def credentials_for(self, tool: ConnectorActionTool) -> dict[str, object]:
            return {"token": "decrypted::cipher-blob"}

        async def dispatch(self, tool, *, credentials, kwargs):  # type: ignore[no-untyped-def]
            self.dispatched.append({"tool": tool, "kwargs": kwargs})
            return {"echo": kwargs}

    provider = _FakeProvider([tool])
    llm = ScriptedLlm(
        [
            LoopTurn(
                content="posting",
                tool_calls=(_tc("slack__post_message", text="hi"),),
            ),
            LoopTurn(
                content="",
                tool_calls=(
                    _declare_command("test -f out.txt"),
                    _tc("file_write", path="out.txt", content="x"),
                ),
            ),
            LoopTurn(content="done", tool_calls=()),
        ]
    )
    async with memory_session() as session:
        # safe_mode=True — pre-0c this was the gating condition. Post-0c
        # the gate is gone; the call must dispatch directly.
        session.add(WorkspaceRow(id=ws, name="ws", region="us-1", safe_mode=True))
        await session.flush()
        run = await _make_run(session, workspace_id=ws)
        orch = RunOrchestrator(
            session=session,
            llm=llm,
            sandbox_manager=NoopSandboxManager(),
            connector_actions=provider,
        )
        result = await orch.run(run=run, workspace_dir=tmp_path)

        assert result.outcome == "verified"
        # The action dispatched — no gate left to block it.
        assert provider.dispatched, (
            "Lift 0c — the Safe Mode danger gate is removed; the connector "
            "action must dispatch directly."
        )
        # No connector_action_approval Decision was created.
        decisions = (await session.execute(select(Decision))).scalars().all()
        approval = [d for d in decisions if d.decision == "connector_action_approval"]
        assert approval == [], (
            "Lift 0c — no connector_action_approval Decision can be created "
            "(the static analyzer that fed it is gone)."
        )


def test_workers_run_does_not_load_connector_plugins_with_analyzer() -> None:
    """Surface delta: ``backend.workflow.infrastructure.workers.run`` no longer imports
    ``DangerAnalyzer`` nor exposes ``load_connector_plugins`` returning a
    danger_map tuple."""
    import backend.workflow.infrastructure.workers.run as mod

    src = inspect.getsource(mod)
    assert "DangerAnalyzer" not in src
    assert "danger_map" not in src
    assert "connector_danger_map" not in src
