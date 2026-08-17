"""Every declared caller is a caller — the registry may not advertise fiction.

The registry's own contract (module docstring): *"A caller is any code site that
invokes an LLM through the dispatch mechanism."* Rule authoring trusts that
contract completely — ``_validate_caller_id`` accepts any ``KNOWN_CALLERS``
member, and the NL compiler is handed the same list as its menu of legal
targets. So a caller_id in this registry is a PROMISE to the founder: "you may
route this, and it will fire."

Two of ten broke that promise. ``workflow.agent_loop.plan`` and
``knowledge.query`` were declared with full specs — timeouts, required methods,
descriptions naming the exact call site — and never wired. The founder wrote
`design (plan) → opus` against one of them; it silently did nothing for months,
across three commits that touched the registry without noticing.

Nothing was watching. This is that watcher. It compares DECLARED callers against
callers the runtime actually dispatches, so the next un-wired spec fails at PR
time instead of becoming a rule that quietly never fires.
"""

from __future__ import annotations

import ast
import pathlib

from backend.dispatch.caller_registry import KNOWN_CALLERS

_BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"
_REGISTRY = _BACKEND / "dispatch" / "caller_registry.py"


def _dispatched_caller_ids() -> set[str]:
    """Caller ids passed as a ``caller_id=`` argument anywhere in ``backend/``.

    Parsed from the AST rather than grepped so a mention inside a docstring or a
    comment does not count as wiring — the failure mode here is precisely a
    caller that is *described* but never *called*.

    Both spellings count: the constant (``caller_id=CALLER_JUDGE``) and a raw
    literal (``caller_id="workflow.judge"``).
    """
    const_to_value: dict[str, str] = {}
    registry_tree = ast.parse(_REGISTRY.read_text(encoding="utf-8"))
    for node in registry_tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("CALLER_"):
                    if isinstance(node.value.value, str):
                        const_to_value[target.id] = node.value.value

    dispatched: set[str] = set()
    for path in _BACKEND.rglob("*.py"):
        if path == _REGISTRY:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a file we cannot parse is not wiring
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "caller_id":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    dispatched.add(kw.value.value)
                elif isinstance(kw.value, ast.Name) and kw.value.id in const_to_value:
                    dispatched.add(const_to_value[kw.value.id])
    return dispatched


def test_the_ast_scan_actually_finds_wiring() -> None:
    """Positive control.

    An empty / broken scanner would make the real assertion below pass
    vacuously — "no caller is unwired" because it found nothing at all. Pin a
    caller we KNOW is dispatched so a scanner regression fails here, loudly,
    instead of silently disarming the gate.
    """
    dispatched = _dispatched_caller_ids()
    assert "workflow.agent_loop.act" in dispatched
    assert "workflow.judge" in dispatched
    assert len(dispatched) >= 5, f"scanner found only {dispatched} — it is broken, not the registry"


def test_every_declared_caller_is_actually_dispatched() -> None:
    """No caller_id may be offered to the founder unless the runtime calls it.

    If this fails you have two honest options — WIRE the call site, or DELETE
    the spec. Do not add an exemption list: an exemption is exactly the state
    this test exists to forbid (a declared caller that never fires, which the
    rule surface still happily accepts).
    """
    declared = set(KNOWN_CALLERS)
    dispatched = _dispatched_caller_ids()
    orphans = sorted(declared - dispatched)

    assert not orphans, (
        f"registry declares {orphans} but nothing in backend/ dispatches them. "
        "The rule surface offers these to the founder as routable, so a rule "
        "written against one silently never fires. Wire the call site or delete "
        "the spec."
    )
