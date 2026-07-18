"""INV-1 enforcement — the Channel registry meta-tests.

Two structural checks make the "orphaned half" defect a build failure:

(a) **Completeness** — every declared channel names at least one producer and
    one consumer, and a human-origin channel names its authoring surface.
    ``producers=()`` / ``consumers=()`` is un-mergeable by construction.

(b) **Producer guard** — no module writes a channel's row through a bare
    ``.add(...)`` outside the sanctioned channel seam. The only legal write
    path is ``CHANNEL.emit(...)``; a bare ``session.add(TriggerEventRow(...))``
    (or ``session.add(row)`` where ``row`` is a ``TriggerEventRow``) bypasses
    the producer assertion and is forbidden.

The guard parses source with ``ast`` rather than importing (mirroring
``tests/test_dunder_all_coverage.py``) so a syntactically broken file flags
here instead of crashing collection.
"""

from __future__ import annotations

import ast
from pathlib import Path

from backend.channels.registry import ALL_CHANNELS

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
PLUGIN = REPO_ROOT / "plugin"

# Production roots the producer guard walks. ``backend`` holds no inline
# tests (they live under top-level ``tests/``); ``plugin`` co-locates its
# tests under ``plugin/*/tests/``, which the walk skips so the guard scans
# only production code — a test fixture may legitimately ``.add`` a row.
_GUARD_ROOTS = (BACKEND, PLUGIN)

# Paths where a ``.add`` of a channel row legitimately lives — the channel
# core wraps the write in ``emit`` (``repo.add(row)``). Everything else must
# route through ``CHANNEL.emit``.
_ADD_ALLOWLIST = frozenset(
    {
        BACKEND / "channels" / "_core.py",
    }
)


def test_all_channels_declare_producers_and_consumers() -> None:
    assert ALL_CHANNELS, "ALL_CHANNELS is empty — declare at least one channel"
    for ch in ALL_CHANNELS:
        assert ch.producers, f"channel {ch.name!r} declares no producers (un-mergeable)"
        assert ch.consumers, f"channel {ch.name!r} declares no consumers (un-mergeable)"
        if ch.human_origin:
            assert ch.authoring_surface, (
                f"channel {ch.name!r} is human-origin but declares no authoring_surface"
            )


def _forbidden_bound_names(
    func: ast.FunctionDef | ast.AsyncFunctionDef, forbidden: set[str]
) -> set[str]:
    """Names inside ``func`` that hold a forbidden row: params annotated as a
    forbidden type, or locals assigned a forbidden-row construction."""
    names: set[str] = set()
    all_args = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
    if func.args.vararg:
        all_args.append(func.args.vararg)
    if func.args.kwarg:
        all_args.append(func.args.kwarg)
    for arg in all_args:
        if isinstance(arg.annotation, ast.Name) and arg.annotation.id in forbidden:
            names.add(arg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _is_forbidden_call(node.value, forbidden):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _is_forbidden_call(node: ast.expr | None, forbidden: set[str]) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden
    )


class _AddGuard(ast.NodeVisitor):
    """Flag ``X.add(<forbidden row>)`` outside a channel seam.

    Scope-aware: a ``.add(name)`` is a violation when ``name`` is bound to a
    forbidden row in an enclosing function (annotated param or local
    construction), or when the argument constructs a forbidden row inline.
    """

    def __init__(self, forbidden: set[str]) -> None:
        self._forbidden = forbidden
        self._scopes: list[set[str]] = []
        self.violations: list[int] = []

    def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scopes.append(_forbidden_bound_names(node, self._forbidden))
        self.generic_visit(node)
        self._scopes.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr == "add" and node.args:
            arg = node.args[0]
            in_scope = {n for scope in self._scopes for n in scope}
            if _is_forbidden_call(arg, self._forbidden) or (
                isinstance(arg, ast.Name) and arg.id in in_scope
            ):
                self.violations.append(node.lineno)
        self.generic_visit(node)


def test_no_bare_add_of_channel_row_outside_allowlist() -> None:
    forbidden = {ch.row.__name__ for ch in ALL_CHANNELS}
    assert forbidden, "no channel rows to guard"

    offenders: list[str] = []
    for root in _GUARD_ROOTS:
        for path in root.rglob("*.py"):
            if path in _ADD_ALLOWLIST or "migrations" in path.parts or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            guard = _AddGuard(forbidden)
            guard.visit(tree)
            offenders.extend(f"{path.relative_to(REPO_ROOT)}:{line}" for line in guard.violations)

    assert not offenders, (
        "INV-1: channel rows may only be written via CHANNEL.emit(...). "
        "Bare .add(...) of a declared channel row found at:\n  - " + "\n  - ".join(offenders)
    )
