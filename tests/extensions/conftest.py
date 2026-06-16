"""Auto-register the audit subscriber for every extensions test.

The audit subscriber is wired explicitly in prod (`backend.api.main` /
`runtime.lifecycle`) — never via module import side-effect. Without this
fixture every `safe_emit` published into the EventBus has no listener
and audit-outbox assertions fail with `[]`. Mirrors
`plugin/audit/tests/conftest.py` so the EventBus is reset before AND
after each test (the bus is a process-wide singleton).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from backend.extensions.eventbus import reset_event_bus_for_testing
from plugin.audit import register_audit_subscriber


@pytest.fixture(autouse=True)
def _audit_subscriber_registered() -> Iterator[None]:
    reset_event_bus_for_testing()
    register_audit_subscriber()
    yield
    reset_event_bus_for_testing()
