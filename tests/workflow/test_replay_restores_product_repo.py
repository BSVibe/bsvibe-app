"""``load_replay_input`` restores an evicted product repo from its bundle (the repo is a cache)."""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.config import get_settings
from backend.data.rls import workspace_session_scope
from backend.storage.product_workspace import (
    init_product_workspace,
    product_workspace_path,
    publish_product_bundle,
)
from backend.workflow.application.adversarial_replay import (
    CaseUnavailable,
    load_replay_input,
)
from backend.workflow.infrastructure.db import (
    Deliverable,
    DeliverableType,
    ExecutionRun,
    RunStatus,
)

from .._support import shared_file_sessionmaker

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "product_bundle_backend", "local", raising=False)
    monkeypatch.setattr(s, "product_bundle_local_root", str(tmp_path / "bundles"), raising=False)


async def _seed(sf: Any, product_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    ws, run_id, did = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sf() as s:
        async with workspace_session_scope(s, ws):
            s.add(
                ExecutionRun(
                    id=run_id,
                    workspace_id=ws,
                    product_id=product_id,
                    status=RunStatus.SHIPPED,
                    payload={"intent_text": "x"},
                    created_at=datetime.now(tz=UTC),
                    updated_at=datetime.now(tz=UTC),
                )
            )
            await s.flush()
            s.add(
                Deliverable(
                    id=did,
                    run_id=run_id,
                    workspace_id=ws,
                    deliverable_type=DeliverableType.DIRECT_OUTPUT,
                    payload={"diff": "diff --git a/a b/a\n"},
                    created_at=datetime.now(tz=UTC),
                )
            )
            await s.flush()
        await s.commit()
    return ws, did


async def _load(sf: Any, ws: uuid.UUID, did: uuid.UUID):
    async with sf() as s:
        async with workspace_session_scope(s, ws):
            return await load_replay_input(s, ws, did)


async def test_a_product_with_only_a_bundle_is_restored_not_skipped() -> None:
    pid = uuid.uuid4()
    await init_product_workspace(pid)
    assert (await publish_product_bundle(pid)).published
    shutil.rmtree(product_workspace_path(pid))  # what the idle reaper does
    async with shared_file_sessionmaker() as sf:
        ws, did = await _seed(sf, pid)
        inp = await _load(sf, ws, did)

    repo = product_workspace_path(pid)
    assert inp.repo_dir == repo and (repo / ".git").exists()
    assert (repo / ".git" / "HEAD").is_file()


async def test_a_product_with_neither_repo_nor_bundle_is_unavailable() -> None:
    pid = uuid.uuid4()
    async with shared_file_sessionmaker() as sf:
        ws, did = await _seed(sf, pid)
        with pytest.raises(CaseUnavailable):
            await _load(sf, ws, did)


async def test_a_restore_failure_is_that_cases_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    pid = uuid.uuid4()

    async def boom(_pid: uuid.UUID) -> bool:
        raise OSError("store down")

    monkeypatch.setattr("backend.storage.product_workspace.ensure_product_workspace", boom)
    async with shared_file_sessionmaker() as sf:
        ws, did = await _seed(sf, pid)
        with pytest.raises(CaseUnavailable, match="store down"):
            await _load(sf, ws, did)
