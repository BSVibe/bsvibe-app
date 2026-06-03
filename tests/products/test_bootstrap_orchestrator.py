"""Orchestrator integration — composes all four stages with a fake Knowledge."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from backend.knowledge.facade import (
    CanonRetrievalQuery,
    CanonRetrievalResult,
    IngestRequest,
    IngestResult,
)
from backend.products.application.bootstrap.orchestrator import (
    BootstrapTooLargeError,
    run_repo_bootstrap,
)


@dataclass
class _FakeKnowledge:
    """Knowledge Protocol stub — captures the IngestRequest it received."""

    received: list[IngestRequest] = field(default_factory=list)

    async def ingest(self, request: IngestRequest) -> IngestResult:
        self.received.append(request)
        return IngestResult(
            proposals_count=0,
            notes_count=len(request.artifacts),
            run_id=uuid.uuid4(),
        )

    async def retrieve_canon(self, query: CanonRetrievalQuery) -> CanonRetrievalResult:
        del query
        return CanonRetrievalResult(notes=[])

    async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int:
        del workspace_id, region
        return 0


def _make(root: Path, rel: str, body: bytes = b"x\n") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)


@pytest.mark.asyncio
async def test_orchestrator_assembles_and_ingests(tmp_path):
    _make(tmp_path, "README.md", b"# my repo\n")
    _make(tmp_path, "pyproject.toml", b"[project]\nname='x'\n")
    _make(tmp_path, "backend/app.py", b"def main():\n    pass\n")
    _make(tmp_path, "apps/pwa/main.ts", b"export const x = 1\n")
    # noise — must be filtered
    _make(tmp_path, "node_modules/foo.js", b"x=1\n")
    _make(tmp_path, "logo.png", b"head\x00tail")

    workspace = uuid.uuid4()
    knowledge = _FakeKnowledge()
    outcome = await run_repo_bootstrap(
        repo_root=tmp_path,
        workspace_id=workspace,
        region="us-1",
        knowledge=knowledge,
    )

    assert len(knowledge.received) == 1
    req = knowledge.received[0]
    assert req.workspace_id == workspace
    assert req.region == "us-1"

    arts: list[dict[str, Any]] = req.artifacts
    # 1 file-tree + 1 manifest + 1 doc + 2 source = 5
    assert outcome.artifacts_count == 5
    assert len(arts) == 5
    kinds = [a["kind"] for a in arts]
    # Order is deterministic: file-tree, manifests, docs, sources.
    assert kinds == ["file-tree", "manifest", "doc", "source", "source"]


@pytest.mark.asyncio
async def test_orchestrator_handles_empty_repo(tmp_path):
    workspace = uuid.uuid4()
    knowledge = _FakeKnowledge()
    outcome = await run_repo_bootstrap(
        repo_root=tmp_path,
        workspace_id=workspace,
        region="us-1",
        knowledge=knowledge,
    )
    assert outcome.artifacts_count == 1  # empty-repo sentinel artifact
    assert len(knowledge.received) == 1


@pytest.mark.asyncio
async def test_orchestrator_propagates_too_large(tmp_path):
    # The orchestrator uses the walker's defaults; assert the walker raises
    # for the same fixture (the orchestrator catches no error here — it
    # propagates), then prove the orchestrator passes it through when the
    # walker hits the cap. We achieve the latter by monkey-patching the
    # imported walker symbol on the orchestrator module to a capped fn.
    from backend.products.application.bootstrap import orchestrator as orch_mod
    from backend.products.application.bootstrap.walker import walk_repo

    _make(tmp_path, "f1.py", b"x\n")
    _make(tmp_path, "f2.py", b"x\n")

    def _capped(repo_root, **_):
        yield from walk_repo(repo_root, max_file_count=1)

    workspace = uuid.uuid4()
    knowledge = _FakeKnowledge()
    original = orch_mod.walk_repo
    orch_mod.walk_repo = _capped  # type: ignore[assignment]
    try:
        with pytest.raises(BootstrapTooLargeError):
            await run_repo_bootstrap(
                repo_root=tmp_path,
                workspace_id=workspace,
                region="us-1",
                knowledge=knowledge,
            )
    finally:
        orch_mod.walk_repo = original  # type: ignore[assignment]
    assert knowledge.received == []
