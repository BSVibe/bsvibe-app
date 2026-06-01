"""Lift N defensive pattern #3 — module-level mutable state regression guard.

The v5 audit flagged three module-level mutable globals that Lift N
collapsed into ``Final[]`` instance holders:

* ``backend.workers.emit._EMIT_CLIENT_CACHE`` → ``_EMIT_CACHE`` instance.
* ``backend.workflow.infrastructure.advisory_lock._FALLBACK_LOCKS`` /
  ``_FALLBACK_HOLDERS`` / ``_FALLBACK_REGISTRY_LOCK`` → ``_FALLBACK``
  instance.
* ``backend.workflow.infrastructure.lease._FALLBACK_LOCKS`` /
  ``_FALLBACK_REGISTRY_LOCK`` → ``_FALLBACK`` instance.

This test pins those names — re-introducing the rebound globals would
fail it immediately. (v8 §22 #3 / D45.)
"""

from __future__ import annotations

from backend.workers import emit as emit_mod
from backend.workflow.infrastructure import advisory_lock as lock_mod
from backend.workflow.infrastructure import lease as lease_mod


def test_emit_cache_is_a_final_instance_not_a_rebound_global() -> None:
    """``backend.workers.emit`` has a single ``_EMIT_CACHE`` instance and
    no longer exposes the pre-Lift-N list-as-cache module global."""
    assert hasattr(emit_mod, "_EMIT_CACHE")
    assert not hasattr(emit_mod, "_EMIT_CLIENT_CACHE"), (
        "Lift N #3 regression: module-level _EMIT_CLIENT_CACHE was reintroduced."
    )
    # The instance carries the cache state — its binding never rebinds.
    cache = emit_mod._EMIT_CACHE
    assert hasattr(cache, "get") and hasattr(cache, "set") and hasattr(cache, "reset")


def test_advisory_lock_fallback_is_a_final_instance() -> None:
    """``backend.workflow.infrastructure.advisory_lock`` no longer exposes
    the three pre-Lift-N module globals."""
    assert hasattr(lock_mod, "_FALLBACK")
    for legacy in ("_FALLBACK_LOCKS", "_FALLBACK_HOLDERS", "_FALLBACK_REGISTRY_LOCK"):
        assert not hasattr(lock_mod, legacy), (
            f"Lift N #3 regression: module-level {legacy} was reintroduced."
        )


def test_lease_fallback_is_a_final_instance() -> None:
    """``backend.workflow.infrastructure.lease`` no longer exposes the
    pre-Lift-N module globals."""
    assert hasattr(lease_mod, "_FALLBACK")
    for legacy in ("_FALLBACK_LOCKS", "_FALLBACK_REGISTRY_LOCK"):
        assert not hasattr(lease_mod, legacy), (
            f"Lift N #3 regression: module-level {legacy} was reintroduced."
        )
