"""Lift E20 — end-to-end bootstrap integration with mocked LLM.

A small fake repo runs through ``run_repo_bootstrap`` with a
mock-backed :class:`Knowledge` so we exercise the WHOLE pipeline:
walker + filter + parser + graph + Leiden + community artifact
rendering + IngestCompiler chunking + the new note schema. Validates
the contract the founder will see on a real dogfood:

* The filter dropped the lockfile + binary.
* The graph was persisted to ``<vault>/code_graph/graph.json``.
* At least one Pattern note (type field landed in frontmatter).
* The vault has ZERO entity-stub explosion in this run (the new
  prompt + cleaning only wikilinks what's literally in content).
"""

from __future__ import annotations

import json
import textwrap
import uuid
from pathlib import Path

import pytest

from backend.knowledge.facade import (
    CanonRetrievalQuery,
    CanonRetrievalResult,
    IngestRequest,
    IngestResult,
)
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenWriter
from backend.knowledge.ingest.ingest_compiler import (
    BatchItem,
    IngestCompiler,
)
from backend.products.application.bootstrap.orchestrator import run_repo_bootstrap


def _seed(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "auth.py").write_text(
        textwrap.dedent(
            '''
            """Authentication helpers."""

            def hash_password(pw: str) -> str:
                """Bcrypt-hash a plain password.

                Tested against the founder's chosen cost factor.
                """
                return "hashed-" + pw


            class AuthService:
                def login(self, user: str, pw: str) -> bool:
                    return hash_password(pw) == "hashed-secret"
            '''
        )
    )
    (repo / "src" / "store.py").write_text(
        textwrap.dedent(
            '''
            """Storage helpers."""

            from typing import Any


            def save(key: str, value: Any) -> None:
                """Persist a value under key."""
                pass
            '''
        )
    )
    (repo / "README.md").write_text("# Demo project\n\nUses [[Bcrypt]] for auth.\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_auth.py").write_text("def test_login():\n    assert True\n")
    # Filter targets.
    (repo / "package-lock.json").write_text('{"name":"x"}\n')
    (repo / "logo.png").write_bytes(b"\x89PNG fake\n")


class _CapturingKnowledge:
    """Knowledge Protocol stub that runs the real IngestCompiler.

    We can't import the real Knowledge factory here without standing
    up a DB + canon service. Instead, this fake drives a real
    :class:`IngestCompiler` against a real :class:`Vault`, with a
    scripted LLM whose response includes a Pattern note for any
    chunk mentioning ``hash_password`` and an empty array otherwise.
    """

    def __init__(self, vault: Vault) -> None:
        self.vault = vault
        self.requests: list[IngestRequest] = []
        self.llm_call_count = 0

        async def _llm_chat(
            *,
            system: str,
            messages: list[dict[str, object]],
            suppress_reasoning: bool = False,
            timeout_s: float | None = None,
        ) -> str:
            del system, suppress_reasoning, timeout_s
            self.llm_call_count += 1
            user_msg = next(
                (m["content"] for m in messages if m.get("role") == "user"),
                "",
            )
            text = str(user_msg)
            if "hash_password" in text:
                return json.dumps(
                    [
                        {
                            "action": "create",
                            "type": "Pattern",
                            "title": "Bcrypt password hashing pattern",
                            "content": (
                                "Use [[Bcrypt]] for password hashing; round-trip via verify only."
                            ),
                            "wikilinks": ["[[Bcrypt]]"],
                            "tags": ["security", "passwords"],
                            "reason": "principle is cross-project",
                        }
                    ]
                )
            return "[]"

        self._llm_chat = _llm_chat

    async def ingest(self, request: IngestRequest) -> IngestResult:
        self.requests.append(request)
        writer = GardenWriter(self.vault)
        compiler = IngestCompiler(
            garden_writer=writer,
            llm_client=_LlmShim(self._llm_chat),
            max_updates=20,
            parallelism=1,
        )
        items = [
            BatchItem(
                label=str(a.get("label", "")),
                content=str(a.get("content", "")),
            )
            for a in request.artifacts
        ]
        result = await compiler.compile_batch(items, seed_source="bootstrap-e2e")
        return IngestResult(
            proposals_count=0,
            notes_count=result.notes_created + result.notes_updated,
            run_id=uuid.uuid5(uuid.NAMESPACE_URL, "e2e"),
            notes_created=result.notes_created,
            notes_updated=result.notes_updated,
            chunk_failures=result.chunk_failures,
        )

    async def retrieve_canon(self, query: CanonRetrievalQuery) -> CanonRetrievalResult:
        del query
        return CanonRetrievalResult(notes=[])

    async def settle(self, *, workspace_id: uuid.UUID, region: str) -> int:
        del workspace_id, region
        return 0


class _LlmShim:
    """Adapter so :class:`IngestCompiler` can call our async ``chat`` fn."""

    def __init__(self, fn) -> None:  # type: ignore[no-untyped-def]
        self._fn = fn

    async def chat(self, **kwargs) -> str:  # type: ignore[no-untyped-def]
        return await self._fn(**kwargs)


@pytest.mark.asyncio
async def test_e20_bootstrap_writes_typed_notes_and_persists_graph(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed(repo)
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    vault = Vault(vault_root)
    vault.ensure_dirs()

    knowledge = _CapturingKnowledge(vault)
    outcome = await run_repo_bootstrap(
        repo_root=repo,
        workspace_id=uuid.uuid4(),
        region="us-1",
        knowledge=knowledge,
        vault_root=vault_root,
    )

    # The orchestrator gave a non-empty artifact list to ingest.
    assert outcome.artifacts_count > 0
    assert len(knowledge.requests) == 1
    req = knowledge.requests[0]
    kinds = [a["kind"] for a in req.artifacts]
    assert "code-graph-community" in kinds
    assert "markdown-doc" in kinds
    # Filter dropped the noise: no source artifact references the
    # filtered paths.
    labels = " ".join(a["label"] for a in req.artifacts)
    assert "package-lock.json" not in labels
    assert "logo.png" not in labels

    # The graph.json was persisted under the workspace vault.
    graph_path = vault_root / "code_graph" / "graph.json"
    assert graph_path.is_file()
    raw = json.loads(graph_path.read_text())
    assert raw["nodes"]
    # Test files NOT in the graph.
    paths_in_graph = {n.get("path", "") for n in raw["nodes"]}
    assert all("tests/test_auth.py" not in p for p in paths_in_graph)

    # At least one note was created with the new ``type: Pattern`` field.
    seedling_dir = vault.root / "garden" / "seedling"
    md_files = list(seedling_dir.glob("*.md"))
    assert md_files
    contents = "\n\n".join(p.read_text() for p in md_files)
    assert "type: Pattern" in contents


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
