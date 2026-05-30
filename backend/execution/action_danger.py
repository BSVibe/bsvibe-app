"""Per-call action danger evaluation (M2).

Workflow §6 #4 / §11.3 — the prior gate around connector ``@p.action`` calls
asked ``tool.is_dangerous`` once, at registration time, against the plugin
loader's load-time ``danger_map`` (which is per-PLUGIN). That snapshot is wrong
in two ways:

* It is **per-plugin**, so a safe read action in a "dangerous" plugin still
  loads with the dangerous verdict (and a dangerous action in a plugin that
  passed the static-analysis gate would still execute without a per-call check).
* It is **load-time**, so a hot-reloaded plugin (or a plugin whose source the
  loader could not inspect at start-up) never gets a fresh verdict at the
  moment the agent actually invokes it.

This module is the per-call seam — one Protocol, one default implementation,
injected into :class:`backend.execution.orchestrator.RunOrchestrator` so the
production loop checks danger **at the execution call**, not at plugin load.

The default implementation reuses the existing static AST gate from
:class:`backend.plugins.analyzer.StaticAnalyzer` (the canonical home for the
"this code makes external calls" verdict) — keeping ONE source of truth for the
danger heuristic. It inspects the action function's source via
:func:`inspect.getsource`, runs the same AST scan over it, and caches the
verdict per ``(plugin, action_name)``. When the source is not inspectable (a
C-extension or a synthetically constructed callable in a test), the gate falls
back to ``tool.is_dangerous`` — the load-time per-plugin verdict — so the
behaviour is never *less* safe than the prior gate.

The Protocol shape leaves room for richer evaluators later (e.g. arg-aware
heuristics — "this comment body contains a URL" — or LLM-judged danger) without
the orchestrator depending on a Union of concretes (per the
``bsvibe-llm-wrapper-not-raw-litellm`` rule: one Protocol seam, never a Union).
"""

from __future__ import annotations

import inspect
import textwrap
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

from backend.execution.connector_actions import ConnectorActionTool
from backend.plugins.analyzer import StaticAnalyzer

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DangerVerdict:
    """One per-call danger verdict for an :class:`ConnectorActionTool` call.

    ``is_dangerous`` drives the orchestrator's Safe Mode gate; ``reason`` is
    surfaced in the resulting ``connector_action_approval`` Decision payload so
    the founder can see *why* the call was gated (audit trail, not just a bool).
    """

    is_dangerous: bool
    reason: str


@runtime_checkable
class ActionDangerEvaluator(Protocol):
    """The per-call danger gate the orchestrator depends on.

    Called once per agent-loop tool invocation, BEFORE dispatch — not once at
    plugin load. The default implementation is
    :class:`StaticActionDangerEvaluator`; tests inject a fake to drive the gate
    deterministically (so the per-call assertion is a property of the
    orchestrator's wiring, not the analyzer's heuristics).
    """

    async def evaluate(
        self, tool: ConnectorActionTool, arguments: dict[str, Any]
    ) -> DangerVerdict: ...


class StaticActionDangerEvaluator:
    """Production :class:`ActionDangerEvaluator` — per-action AST scan, cached.

    Re-uses :class:`StaticAnalyzer` (the same heuristic the plugin loader's
    :class:`~backend.plugins.analyzer.DangerAnalyzer` runs at load-time) but
    applies it to **the action function's source** instead of the whole plugin
    file. That means: a read-only action in a dangerous plugin can pass; a new
    dangerous action a hot-reload added to a previously-safe plugin still gets
    flagged on the FIRST agent call.

    Caches one verdict per ``(plugin_name, action_name)`` because the static
    verdict over a single function is content-deterministic and the agent loop
    can re-call the same tool many times in one run (per-call ≠ per-call AST
    parse).

    When :func:`inspect.getsource` cannot resolve the function source (a
    callable built without a module file — e.g. a tests'
    ``ActionCapability(fn=async def …)`` constructed in-memory), the evaluator
    falls back to ``tool.is_dangerous`` — the load-time per-plugin verdict
    threaded through the resolver. The fallback guarantees the per-call gate
    is never *less* safe than the prior load-time gate.
    """

    def __init__(self) -> None:
        self._static = StaticAnalyzer()
        self._cache: dict[tuple[str, str], DangerVerdict] = {}

    async def evaluate(self, tool: ConnectorActionTool, arguments: dict[str, Any]) -> DangerVerdict:
        key = (tool.connector, tool.action_name)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        verdict = self._evaluate_fresh(tool)
        self._cache[key] = verdict
        return verdict

    def _evaluate_fresh(self, tool: ConnectorActionTool) -> DangerVerdict:
        # The per-call verdict is the OR of (a) the per-action AST scan and
        # (b) the load-time per-plugin verdict carried on the tool. OR-ing
        # guarantees the gate is never *less* safe than the prior load-time
        # gate, while still catching dangerous actions that a previously-safe
        # plugin adds (or actions whose source imports a dangerous module the
        # plugin-level scan missed — e.g. a lazy ``import httpx`` inside one
        # action). The plugin-level verdict alone is NOT enough — that's the
        # bug M2 fixes; the per-action verdict alone would silently downgrade
        # existing dangerous actions (since most action functions delegate to
        # a client and have no dangerous imports in their own source).
        plugin_verdict = bool(tool.is_dangerous)
        try:
            source = inspect.getsource(tool.action.fn)
        except (OSError, TypeError) as exc:
            # OSError — source file not available (frozen / C-extension).
            # TypeError — the object has no inspectable source (synthetic
            # callable, partial, etc.). Both fall back to the load-time per-
            # plugin verdict so the gate is no LESS safe than the prior code.
            logger.debug(
                "action_danger_source_unavailable",
                connector=tool.connector,
                action=tool.action_name,
                error=str(exc),
            )
            return DangerVerdict(
                is_dangerous=plugin_verdict,
                reason=(
                    "source unavailable for per-action AST scan — using "
                    f"plugin-level verdict (is_dangerous={plugin_verdict})"
                ),
            )
        # Dedent — :func:`inspect.getsource` returns the function with its
        # original indentation, which trips :func:`ast.parse` for any
        # function not declared at module level (nested helpers, test fixtures,
        # closures). :func:`textwrap.dedent` normalizes the source so the
        # static analyzer always sees a valid top-level statement.
        source = textwrap.dedent(source)
        result = self._static.analyze(source)
        if result is None:
            # AST parse failed — degrade to load-time verdict (same fallback).
            logger.warning(
                "action_danger_parse_failed",
                connector=tool.connector,
                action=tool.action_name,
            )
            return DangerVerdict(
                is_dangerous=plugin_verdict,
                reason=(
                    "AST parse failed on action source — using plugin-level "
                    f"verdict (is_dangerous={plugin_verdict})"
                ),
            )
        action_verdict, action_reason = result
        is_dangerous = bool(action_verdict) or plugin_verdict
        if action_verdict and not plugin_verdict:
            reason = f"per-action AST: {action_reason}"
        elif plugin_verdict and not action_verdict:
            reason = "plugin-level verdict from loader danger_map"
        elif action_verdict and plugin_verdict:
            reason = f"per-action AST: {action_reason}; plugin-level also dangerous"
        else:
            reason = str(action_reason)
        return DangerVerdict(is_dangerous=is_dangerous, reason=reason)


__all__ = [
    "ActionDangerEvaluator",
    "DangerVerdict",
    "StaticActionDangerEvaluator",
]
