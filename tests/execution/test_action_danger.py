"""Per-call ActionDangerEvaluator tests (M2).

Covers the per-call seam :class:`backend.execution.action_danger.ActionDangerEvaluator`:

* The default :class:`StaticActionDangerEvaluator` runs the StaticAnalyzer over
  the action function's source — a per-action verdict, not the per-plugin
  snapshot the loader caches at start-up.
* OR-semantics with the load-time plugin verdict — never *less* safe than the
  prior gate.
* Cached per ``(plugin, action_name)`` (one AST parse per pair, not per call).
* Source-unavailable / parse-fail / evaluator-raise fall back fail-safe.
* The Protocol is runtime-checkable so the orchestrator's dependency seam
  stays one interface (no Union of concretes).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from backend.connectors.db import ConnectorAccountRow
from backend.execution.action_danger import (
    ActionDangerEvaluator,
    DangerVerdict,
    StaticActionDangerEvaluator,
)
from backend.execution.connector_actions import ConnectorActionTool
from backend.plugins.base import ActionCapability, PluginMeta


def _plugin(name: str, actions: dict[str, ActionCapability]) -> PluginMeta:
    return PluginMeta(
        name=name,
        version="0",
        description="",
        author="t",
        data_jurisdiction="us",
        credentials=[],
        actions=actions,
    )


def _account(connector: str) -> ConnectorAccountRow:
    return ConnectorAccountRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        connector=connector,
        webhook_token="t",
        signing_secret_ciphertext="x",
        delivery_config={},
        is_active=True,
    )


def _tool(meta: PluginMeta, action_name: str, *, is_dangerous: bool) -> ConnectorActionTool:
    return ConnectorActionTool(
        plugin=meta,
        action=meta.actions[action_name],
        account=_account(meta.name),
        is_dangerous=is_dangerous,
    )


# ── Protocol ───────────────────────────────────────────────────────────────


def test_default_evaluator_satisfies_protocol() -> None:
    assert isinstance(StaticActionDangerEvaluator(), ActionDangerEvaluator)


# ── real GitHub list_issues action (the new M2 read action) ──────────────────


async def test_real_github_list_issues_action_is_not_dangerous() -> None:
    """The newly-added github ``list_issues`` action function has no dangerous
    imports in its source — the per-action AST scan must say SAFE. (Plugin-level
    is_dangerous may still be True from the loader's per-plugin scan; the
    OR-rule means the OVERALL verdict is dangerous in that case — see the
    explicit OR-rule tests below.)"""
    from backend.plugins.implementations.github import plugin as github_module

    p = github_module.p
    action = p.meta.actions["list_issues"]
    tool = ConnectorActionTool(
        plugin=p.meta,
        action=action,
        account=_account("github"),
        is_dangerous=False,  # simulate a plugin the loader flagged safe
    )
    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(tool, arguments={"repo": "o/r"})
    assert verdict.is_dangerous is False, (
        f"list_issues body has no dangerous imports — got {verdict}"
    )


async def test_real_sentry_list_issues_action_is_not_dangerous() -> None:
    """Same as the github list_issues test for the sentry list_issues action."""
    from backend.plugins.implementations.sentry import plugin as sentry_module

    p = sentry_module.p
    action = p.meta.actions["list_issues"]
    tool = ConnectorActionTool(
        plugin=p.meta,
        action=action,
        account=_account("sentry"),
        is_dangerous=False,
    )
    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(
        tool,
        arguments={
            "organization_slug": "o",
            "project_slug": "p",
        },
    )
    assert verdict.is_dangerous is False


# ── per-action AST detects dangerous imports in the action body ──────────────


async def test_per_action_detects_dangerous_when_plugin_was_safe() -> None:
    """The audit fix: an action whose OWN source imports a dangerous module
    must be flagged at call time even when the plugin-level snapshot said safe.
    This is the regression the load-time-only gate would silently allow."""

    # NB: the StaticAnalyzer parses the function source body — an `import httpx`
    # inside the function will be picked up by the AST walker.
    async def naughty_action(context: Any, **kwargs: Any) -> dict[str, Any]:
        import httpx  # noqa: F401 — the AST scan sees this and flags dangerous

        return {}

    meta = _plugin(
        "newplugin",
        {"do": ActionCapability(fn=naughty_action, name="do", mcp_exposed=True)},
    )
    tool = _tool(meta, "do", is_dangerous=False)  # plugin-level: SAFE

    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(tool, arguments={})

    assert verdict.is_dangerous is True, (
        "per-action AST scan must flag a new dangerous action even when the "
        f"plugin-level verdict is safe — got {verdict}"
    )
    assert "httpx" in verdict.reason or "External communication" in verdict.reason


async def test_or_rule_plugin_dangerous_safe_action() -> None:
    """When the per-action source is clean BUT the plugin-level verdict says
    dangerous, the OR-rule keeps the overall verdict dangerous — the gate is
    never *less* safe than today."""

    async def quiet_action(context: Any, **kwargs: Any) -> dict[str, Any]:
        # no dangerous imports in this function body
        return {"ok": True}

    meta = _plugin(
        "p",
        {"q": ActionCapability(fn=quiet_action, name="q", mcp_exposed=True)},
    )
    tool = _tool(meta, "q", is_dangerous=True)  # plugin-level: DANGEROUS

    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(tool, arguments={})

    assert verdict.is_dangerous is True, (
        "OR-rule: when plugin-level is dangerous, the verdict stays dangerous "
        f"even with a clean per-action scan — got {verdict}"
    )


async def test_both_safe_keeps_verdict_safe() -> None:
    """The OR-rule lets a clean action in a clean plugin through."""

    async def clean(context: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    meta = _plugin("p", {"c": ActionCapability(fn=clean, name="c", mcp_exposed=True)})
    tool = _tool(meta, "c", is_dangerous=False)

    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(tool, arguments={})
    assert verdict.is_dangerous is False


# ── caching: one AST parse per (plugin, action) ─────────────────────────────


async def test_per_call_evaluation_is_cached() -> None:
    """The evaluator caches one verdict per ``(plugin, action_name)`` so the
    agent loop's repeated calls don't re-parse the same source."""

    async def clean(context: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    meta = _plugin("p", {"a": ActionCapability(fn=clean, name="a", mcp_exposed=True)})
    tool = _tool(meta, "a", is_dangerous=False)

    evaluator = StaticActionDangerEvaluator()
    v1 = await evaluator.evaluate(tool, arguments={"x": 1})
    v2 = await evaluator.evaluate(tool, arguments={"x": 2})
    # Same verdict object (cache hit returns the cached DangerVerdict).
    assert v1 is v2


# ── fallback paths: source unavailable / parse-fail ─────────────────────────


async def test_source_unavailable_falls_back_to_plugin_verdict() -> None:
    """When :func:`inspect.getsource` cannot find the source (a synthetic
    callable / builtin / lambda built in tests without a module file), the
    evaluator falls back to the load-time per-plugin verdict — never *less*
    safe than the prior gate."""

    # A bare ``object()`` is a useful stand-in: inspect.getsource raises
    # TypeError on a non-callable that isn't a class/module/method.
    class _Fake:
        pass

    fake_fn = _Fake()  # not a real function — inspect.getsource will TypeError
    meta = _plugin(
        "p",
        {
            "a": ActionCapability(
                fn=fake_fn,  # type: ignore[arg-type] — deliberate fallback path
                name="a",
                mcp_exposed=True,
            )
        },
    )
    tool = _tool(meta, "a", is_dangerous=True)

    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(tool, arguments={})
    assert verdict.is_dangerous is True, (
        f"source unavailable → fall back to plugin-level dangerous=True; got {verdict}"
    )
    assert "source unavailable" in verdict.reason or "plugin-level" in verdict.reason


async def test_source_unavailable_falls_back_safe_when_plugin_safe() -> None:
    class _Fake:
        pass

    fake_fn = _Fake()
    meta = _plugin(
        "p",
        {
            "a": ActionCapability(
                fn=fake_fn,  # type: ignore[arg-type]
                name="a",
                mcp_exposed=True,
            )
        },
    )
    tool = _tool(meta, "a", is_dangerous=False)
    evaluator = StaticActionDangerEvaluator()
    verdict = await evaluator.evaluate(tool, arguments={})
    assert verdict.is_dangerous is False


# ── Fake evaluator demonstrates the seam ────────────────────────────────────


class FakeEvaluator:
    """A :class:`ActionDangerEvaluator` whose verdict is per-call configurable."""

    def __init__(self, verdict: DangerVerdict) -> None:
        self._verdict = verdict
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def evaluate(self, tool: ConnectorActionTool, arguments: dict[str, Any]) -> DangerVerdict:
        self.calls.append((tool.connector, tool.action_name, dict(arguments)))
        return self._verdict


def test_fake_evaluator_satisfies_protocol() -> None:
    assert isinstance(
        FakeEvaluator(DangerVerdict(is_dangerous=False, reason="t")),
        ActionDangerEvaluator,
    )


@pytest.mark.parametrize("is_dangerous", [True, False])
async def test_fake_evaluator_returns_configured_verdict(is_dangerous: bool) -> None:
    """The Protocol is satisfied by any object with the right shape — confirms
    tests can drive the orchestrator's gate deterministically without depending
    on the StaticAnalyzer heuristics."""

    async def fn(context: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    meta = _plugin("p", {"a": ActionCapability(fn=fn, name="a", mcp_exposed=True)})
    tool = _tool(meta, "a", is_dangerous=False)
    fake = FakeEvaluator(DangerVerdict(is_dangerous=is_dangerous, reason="test"))
    verdict = await fake.evaluate(tool, arguments={"k": "v"})
    assert verdict.is_dangerous is is_dangerous
    assert fake.calls == [("p", "a", {"k": "v"})]
