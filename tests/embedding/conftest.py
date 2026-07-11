"""Shared embedding fixtures — in-memory sqlite session for the embedding tier.

After Lift D's embedding hoist (``backend/router/embedding/`` →
``backend/embedding/``) the test tree mirrors the source tree. Embedding tests
no longer inherit ``tests/router/conftest.py``, so the ``session`` fixture (the
in-memory async sqlite session) lives here alongside the table-registration
imports the embedding tier needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for table-registration side effects on the shared Base.metadata.
import backend.embedding.db  # noqa: F401
import backend.router.accounts.models  # noqa: F401
from tests._support import memory_session


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with memory_session() as s:
        yield s
