"""Bundle 1 smoke test — three modules importable, no circular deps."""

from __future__ import annotations


def test_all_three_modules_import():
    import backend.extensions
    import backend.extensions.plugin
    import backend.router
    import backend.router.accounts
    import backend.workflow.infrastructure.sandbox as sandbox_mod
    import plugin.audit as audit_mod

    # Each module re-exports the contract surface that downstream
    # bundles will depend on.
    assert backend.extensions.plugin.PluginRunner is not None
    assert backend.extensions.plugin.PluginBuilder is not None

    # Audit moved out of supervisor in Lift G — supervisor is sandbox-only.
    # Lift I-0 folded sandbox into the Workflow context's infrastructure
    # layer (per v8 D3 — Sandbox = Verifier internal swappable strategy);
    # backend/supervisor/ no longer exists.
    assert audit_mod.safe_emit is not None
    assert audit_mod.AuditEvent is not None
    assert sandbox_mod.get_sandbox_manager is not None
    assert sandbox_mod.NoopSandboxManager is not None

    # Lift E2 — GatewayDispatcher / LocalVsCloudClassifier removed.
    assert backend.router.LlmClient is not None
    assert backend.router.ModelAccountNotFound is not None
    assert backend.router.accounts.ModelAccountService is not None
    assert backend.router.budget.BudgetPolicyService is not None


def test_no_circular_imports_between_modules():
    """Plugins must not import from gateway or supervisor; supervisor
    must not import from gateway. Gateway may use supervisor (audit)
    in a later bundle, but Bundle 1 keeps them disjoint."""
    import sys

    # Snapshot every already-imported backend.* module so we can put them back
    # verbatim afterwards. Re-importing modules in isolation (below) rebinds the
    # SQLAlchemy model classes (a fresh ``ExecutionRun`` on a new mapper); if we
    # left those fresh duplicates — or the holes from the ``del`` — in
    # sys.modules, a downstream test that lazily re-imports one module would end
    # up with two versions of a model class. The session identity map is keyed
    # on (class, pk), so the two versions never share an instance, and an
    # in-place attribute mutation on one is invisible on the other. Restoring
    # the snapshot keeps the interpreter consistent for every later test.
    saved = {m: sys.modules[m] for m in list(sys.modules) if m.startswith("backend.")}
    try:
        # Clear and reimport in isolation to surface any cross-module loop.
        for mod in saved:
            del sys.modules[mod]

        import backend.extensions.plugin  # noqa: F401

        plugin_modules = {m for m in sys.modules if m.startswith("backend.extensions.plugin")}
        forbidden = {
            m
            for m in sys.modules
            if m.startswith("backend.router.")
            or m.startswith("backend.workflow.infrastructure.sandbox")
        }
        assert not forbidden, f"backend.extensions.plugin pulled in: {forbidden}"
        assert plugin_modules
    finally:
        # Drop everything imported during the isolated probe, then restore the
        # originals so no fresh duplicate class objects linger in sys.modules.
        for mod in [m for m in list(sys.modules) if m.startswith("backend.")]:
            del sys.modules[mod]
        sys.modules.update(saved)
