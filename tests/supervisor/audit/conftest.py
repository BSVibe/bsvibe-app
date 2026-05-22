"""Shared fixtures for supervisor audit tests — in-memory sqlite session."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for table-registration side effects on the shared Base.metadata.
import backend.supervisor.audit.models  # noqa: F401
from tests._support import memory_session


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with memory_session() as s:
        yield s
