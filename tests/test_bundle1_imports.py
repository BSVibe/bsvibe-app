"""Bundle 1 smoke test — three modules importable, no circular deps."""

from __future__ import annotations


def test_all_three_modules_import():
    import backend.extensions
    import backend.extensions.plugin
    import backend.router
    import backend.router.accounts
    import backend.supervisor
    import plugin.audit as audit_mod

    # Each module re-exports the contract surface that downstream
    # bundles will depend on.
    assert backend.extensions.plugin.PluginRunner is not None
    assert backend.extensions.plugin.PluginBuilder is not None

    # Audit moved out of supervisor in Lift G — supervisor is sandbox-only.
    assert audit_mod.safe_emit is not None
    assert audit_mod.AuditEvent is not None
    assert backend.supervisor.sandbox.get_sandbox_manager is not None
    assert backend.supervisor.sandbox.NoopSandboxManager is not None

    assert backend.router.GatewayDispatcher is not None
    assert backend.router.LlmClient is not None
    assert backend.router.accounts.ModelAccountService is not None
    assert backend.router.budget.BudgetPolicyService is not None
    assert backend.router.classifier.LocalVsCloudClassifier is not None


def test_no_circular_imports_between_modules():
    """Plugins must not import from gateway or supervisor; supervisor
    must not import from gateway. Gateway may use supervisor (audit)
    in a later bundle, but Bundle 1 keeps them disjoint."""
    import sys

    # Clear and reimport in isolation to surface any cross-module loop.
    for mod in [m for m in list(sys.modules) if m.startswith("backend.")]:
        del sys.modules[mod]

    import backend.extensions.plugin  # noqa: F401

    plugin_modules = {m for m in sys.modules if m.startswith("backend.extensions.plugin")}
    forbidden = {
        m
        for m in sys.modules
        if m.startswith("backend.router.") or m.startswith("backend.supervisor.")
    }
    assert not forbidden, f"backend.extensions.plugin pulled in: {forbidden}"
    assert plugin_modules
