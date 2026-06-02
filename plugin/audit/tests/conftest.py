"""Shared fixtures for supervisor audit tests — in-memory sqlite session."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for table-registration side effects on the shared Base.metadata.
import plugin.audit.models  # noqa: F401
from backend.extensions.eventbus import reset_event_bus_for_testing
from plugin.audit import register_audit_subscriber
from tests._support import memory_session


@pytest.fixture(autouse=True)
def _audit_subscriber_registered():
    """Register the audit subscriber on a fresh EventBus for every test.

    Mirrors the explicit wiring in :mod:`backend.api.main` /
    :mod:`backend.workflow.application.runtime.lifecycle` so safe_emit-driven
    tests see the same dispatcher path as prod.
    """
    reset_event_bus_for_testing()
    register_audit_subscriber()
    yield
    reset_event_bus_for_testing()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with memory_session() as s:
        yield s
