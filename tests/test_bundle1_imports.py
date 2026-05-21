"""Bundle 1 smoke test — three modules importable, no circular deps."""

from __future__ import annotations


def test_all_three_modules_import():
    import backend.gateway
    import backend.plugins
    import backend.supervisor

    # Each module re-exports the contract surface that downstream
    # bundles will depend on.
    assert backend.plugins.PluginRunner is not None
    assert backend.plugins.PluginBuilder is not None
    assert backend.plugins.DangerAnalyzer is not None

    assert backend.supervisor.safe_emit is not None
    assert backend.supervisor.AuditEvent is not None
    assert backend.supervisor.sandbox.get_sandbox_manager is not None
    assert backend.supervisor.sandbox.NoopSandboxManager is not None

    assert backend.gateway.GatewayDispatcher is not None
    assert backend.gateway.LlmClient is not None
    assert backend.gateway.accounts.ModelAccountService is not None
    assert backend.gateway.budget.BudgetPolicyService is not None
    assert backend.gateway.classifier.LocalVsCloudClassifier is not None


def test_no_circular_imports_between_modules():
    """Plugins must not import from gateway or supervisor; supervisor
    must not import from gateway. Gateway may use supervisor (audit)
    in a later bundle, but Bundle 1 keeps them disjoint."""
    import sys

    # Clear and reimport in isolation to surface any cross-module loop.
    for mod in [m for m in list(sys.modules) if m.startswith("backend.")]:
        del sys.modules[mod]

    import backend.plugins  # noqa: F401

    plugin_modules = {m for m in sys.modules if m.startswith("backend.plugins")}
    forbidden = {
        m
        for m in sys.modules
        if m.startswith("backend.gateway.") or m.startswith("backend.supervisor.")
    }
    assert not forbidden, f"backend.plugins pulled in: {forbidden}"
    assert plugin_modules
