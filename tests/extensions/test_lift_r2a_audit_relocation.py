"""Lift R2a — smoke surface for audit relocation to repo-root ``plugin/audit/``
+ EventBus rewire (v8 §13 Lift R + D38 + D5 audit-as-plugin).

Asserts:
* audit resolves at the NEW repo-root path (``plugin.audit``) with its
  full public surface (``AuditEmitter`` / ``AuditEvent`` / ``AuditActor`` /
  ``AuditResource`` / ``safe_emit`` / ``make_actor`` / ``OutboxStore``).
* the OLD ``backend.extensions.implementations.audit`` path raises
  ``ModuleNotFoundError`` (full retirement, no compat shim).
* the EventBus surface (``InProcessEventBus``) is importable from
  ``backend.extensions.eventbus`` and the audit plugin registers a
  subscriber against the ``audit.`` kind prefix.
* the 7 known ``safe_emit`` call sites in backend production code no
  longer import from the OLD audit path; they import from ``plugin.audit``.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

# Kept as string parts so the global rename sweep can't silently rewrite them.
_OLD = "backend." + "extensions." + "implementations" + ".audit"
_NEW = "plugin.audit"


def test_audit_at_new_repo_root_path() -> None:
    mod = importlib.import_module(_NEW)
    for name in (
        "AuditEmitter",
        "AuditEvent",
        "AuditActor",
        "AuditResource",
        "safe_emit",
        "make_actor",
        "OutboxStore",
    ):
        assert hasattr(mod, name), f"plugin.audit missing re-export: {name}"


def test_audit_old_path_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(_OLD)


def test_in_process_event_bus_importable() -> None:
    mod = importlib.import_module("backend.extensions.eventbus")
    assert hasattr(mod, "InProcessEventBus")
    assert hasattr(mod, "get_event_bus")


def test_audit_plugin_registers_subscriber_on_import() -> None:
    # Importing the audit plugin must register an audit-prefix subscriber on
    # the in-process bus singleton. The subscriber persists audit events to
    # the outbox.
    from backend.extensions.eventbus import get_event_bus

    bus = get_event_bus()
    importlib.import_module(_NEW)
    # The bus exposes a debug surface: ``_prefixes`` returns the prefixes
    # currently registered. ``audit.`` MUST be present after the import.
    assert "audit." in bus.registered_prefixes()


_BACKEND_PRODUCER_SITES = (
    "backend/api/v1/chat.py",
    # C1 extracted the checkpoint-resolve audit producer out of
    # backend/api/v1/checkpoints.py (now a thin caller) into the shared
    # resolve service so the MCP checkpoint tools can reuse it.
    "backend/workflow/application/checkpoint_resolution.py",
    # Lift H2a decomposed the native loop's audit producer out of
    # backend/execution/orchestrator.py (now a thin re-export shim) into
    # backend/workflow/application/run_persistence.py — the canonical home
    # for the loop's DB + audit side effects.
    "backend/workflow/application/run_persistence.py",
    "backend/executors/terminal.py",
)


@pytest.mark.parametrize("rel", _BACKEND_PRODUCER_SITES)
def test_backend_producer_sites_use_new_path(rel: str) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / rel).read_text(encoding="utf-8")
    # NO import from the OLD audit path.
    assert "backend.extensions.implementations.audit" not in text, (
        f"{rel} still imports from old audit path"
    )
    # Must import from plugin.audit (either ``plugin.audit`` package or
    # one of its submodules: ``plugin.audit.events`` / ``plugin.audit.service``).
    assert re.search(r"from plugin\.audit", text), f"{rel} doesn't import from plugin.audit"


def test_r1_marker_audit_still_at_old_path_assertion_removed() -> None:
    # The R1 marker test ``test_audit_still_at_old_path_pending_r2`` must be
    # removed/flipped on the R2 commit per the R1 closeout. We assert here
    # that the SOURCE no longer contains the original marker symbol.
    repo_root = Path(__file__).resolve().parents[2]
    marker_path = repo_root / "tests" / "extensions" / "test_lift_r1_relocation.py"
    if not marker_path.exists():
        return  # already removed wholesale — acceptable
    text = marker_path.read_text(encoding="utf-8")
    assert "test_audit_still_at_old_path_pending_r2" not in text
