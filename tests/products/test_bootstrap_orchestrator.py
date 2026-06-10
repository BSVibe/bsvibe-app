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
    # Lift E20 — the new pipeline emits structural seeds (file-tree +
    # manifest) PLUS one ``markdown-doc`` artifact per markdown file
    # PLUS one ``code-graph-community`` artifact per detected community
    # (Leiden on the AST graph). The exact community count is graph-
    # density-dependent so we assert the SHAPE rather than the count.
    kinds = [a["kind"] for a in arts]
    assert kinds[0] == "file-tree"
    assert "manifest" in kinds
    assert "markdown-doc" in kinds
    assert "code-graph-community" in kinds
    # Lockfile-free, vendor-free: filter dropped the noise BEFORE we
    # got here, so no source artifact comes from ``node_modules/`` or
    # the PNG.
    labels = " ".join(a["label"] for a in arts)
    assert "node_modules" not in labels
    assert "logo.png" not in labels
    assert outcome.artifacts_count == len(arts)


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
async def test_orchestrator_propagates_too_large(tmp_path, monkeypatch):
    # The Lift E20 orchestrator calls walk_repo twice — once inside
    # ``build_code_graph_artifacts`` (lazy-imported there to break the
    # bootstrap → code_graph circular import) and once for the
    # structural-seeds pass. We patch the canonical export in
    # ``backend.products.application.bootstrap.walker``; both call
    # sites resolve to that module so one substitution covers both.
    from backend.products.application.bootstrap import walker as walker_mod

    _make(tmp_path, "f1.py", b"x\n")
    _make(tmp_path, "f2.py", b"x\n")

    original = walker_mod.walk_repo

    def _capped(repo_root, **_):
        yield from original(repo_root, max_file_count=1)

    monkeypatch.setattr(walker_mod, "walk_repo", _capped)
    # The orchestrator module imports walk_repo by name at import time;
    # patch that binding too so the structural pass sees the capped fn.
    from backend.products.application.bootstrap import orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "walk_repo", _capped)

    workspace = uuid.uuid4()
    knowledge = _FakeKnowledge()
    with pytest.raises(BootstrapTooLargeError):
        await run_repo_bootstrap(
            repo_root=tmp_path,
            workspace_id=workspace,
            region="us-1",
            knowledge=knowledge,
        )
    assert knowledge.received == []
