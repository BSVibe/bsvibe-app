"""컴파일러가 볼트를 볼 수 있어야 한다 — 구조 감사 후속.

``IngestCompiler`` 는 청크마다 :func:`find_related` 를 불러 "이미 있는 노트"를 보고
create 냐 update 냐를 정한다. 그런데 프로덕션 생성 지점 둘 다 ``retriever=`` 를 넘기지
않아서 **모든 ingest·settle compile 이 항상 ``"No existing notes available."`` 를 듣고**
판단해 왔다.

감사는 이걸 *"인자를 안 넘겼다"* 로 적었지만 실제로는 한 겹 더 나빴다 — 레포 전체에서
``VaultRetriever(`` 의 프로덕션 생성 지점이 **0개**였다(테스트뿐). 넘길 객체가 만들어진
적이 없다.

⚠️ 처방을 고를 때 세 가지가 걸린다:

1. ``KnowledgeFactory.retriever()`` 는 **다른 프로토콜**(``CanonRetriever``)이라 못 쓴다
2. 빈 ``VaultRetriever(vault)`` 는 **recency 폴백**이라 "관련" 노트가 아니다 — 무관한
   노트를 관련이라 내놓으면 update/create 판단을 틀린 쪽으로 민다
3. ``PgNoteVectorBackend`` 는 **살아 있는 AsyncSession** 을 붙들고, 컴파일러는 청크를
   동시에 돌린다 — 하나의 세션을 공유하면 동시 사용이 된다

✅ prod 실측(2026-08-20): ``note_embeddings`` **1,714행 · 워크스페이스 2 · 최신 08-19**.
producer 는 ``reconcile_embeddings`` 로 살아 있다. 쓸 자산이 이미 있었다.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[3] / "backend"


def _construction_sites() -> dict[str, list[ast.Call]]:
    """Every production ``IngestCompiler(...)`` call, by module path."""
    sites: dict[str, list[ast.Call]] = {}
    for path in _backend_root().rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        if "IngestCompiler(" not in source:
            continue
        calls = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "IngestCompiler"
        ]
        if calls:
            sites[path.relative_to(_backend_root()).as_posix()] = calls
    return sites


def test_the_production_sites_are_the_two_we_know_about() -> None:
    """Pin the set so a NEW construction site is forced through this test — the
    defect was that both known sites forgot the same argument, and a third one
    would forget it the same way."""
    assert set(_construction_sites()) == {
        "workflow/application/runtime/product_bootstrap_runtime.py",
        "workflow/application/runtime/settle_runtime.py",
    }


def test_every_production_compiler_is_given_a_retriever() -> None:
    """Without ``retriever=`` the compiler is structurally blind: ``find_related``
    short-circuits to ``"No existing notes available."`` and every chunk decides
    create-vs-update against nothing."""
    blind = {
        module: [c.lineno for c in calls if not any(k.arg == "retriever" for k in c.keywords)]
        for module, calls in _construction_sites().items()
    }
    blind = {m: lines for m, lines in blind.items() if lines}
    assert not blind, f"these production compilers cannot see the vault: {blind}"


def test_a_bare_vault_retriever_is_not_the_answer() -> None:
    """Guard the SHAPE of the fix, not just its presence.

    ``VaultRetriever(vault)`` with no vector store falls back to recency — it
    returns the most RECENT notes, not the related ones. Handing those to the
    compiler as "existing related notes" is worse than honest silence: it invites
    an update against a note that has nothing to do with the chunk."""
    for module in _construction_sites():
        source = (_backend_root() / module).read_text(encoding="utf-8")
        for call in re.findall(r"VaultRetriever\((?:[^()]|\([^()]*\))*\)", source):
            assert "vector_store" in call, (
                f"{module} builds a recency-only retriever — pass the semantic "
                f"backend the workspace already has: {call}"
            )


# ---------------------------------------------------------------------------
# The builder itself
# ---------------------------------------------------------------------------


def _settings(model: str | None, tmp_path: Path):
    from types import SimpleNamespace

    return SimpleNamespace(knowledge_vault_root=str(tmp_path), knowledge_embedding_model=model)


def test_no_embedding_model_stays_silent_rather_than_guessing(tmp_path: Path) -> None:
    """A recency fallback would answer a DIFFERENT question — "what is newest",
    not "what is related" — and the compiler would update against it."""
    from backend.knowledge.retrieval.ingest_retriever import build_ingest_retriever

    assert (
        build_ingest_retriever(
            settings=_settings(None, tmp_path),  # type: ignore[arg-type]
            session_factory=object(),  # type: ignore[arg-type]
            region="kr",
            workspace_id=uuid.uuid4(),
        )
        is None
    )


def test_no_session_factory_stays_silent(tmp_path: Path) -> None:
    from backend.knowledge.retrieval.ingest_retriever import build_ingest_retriever

    assert (
        build_ingest_retriever(
            settings=_settings("text-embedding-3-small", tmp_path),  # type: ignore[arg-type]
            session_factory=None,
            region="kr",
            workspace_id=uuid.uuid4(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_the_backend_opens_one_session_per_call() -> None:
    """The load-bearing property. ``PgNoteVectorBackend`` holds a live session and
    the compiler searches from CONCURRENT chunk tasks — a shared session would
    interleave. Assert what the concurrency actually needs: N calls, N sessions,
    never one reused across two in-flight calls."""
    import asyncio
    from contextlib import asynccontextmanager

    from backend.knowledge.retrieval.storage.pg import SessionScopedNoteVectorBackend

    live: list[object] = []
    peak = 0
    opened: list[object] = []

    class _Rows:
        def mappings(self):
            return []

    class _Session:
        async def execute(self, *_a, **_k):
            await asyncio.sleep(0)  # yield so concurrent calls actually overlap
            return _Rows()

    @asynccontextmanager
    async def _factory():
        nonlocal peak
        session = _Session()
        opened.append(session)
        live.append(session)
        peak = max(peak, len(live))
        try:
            yield session
        finally:
            live.remove(session)

    backend = SessionScopedNoteVectorBackend(
        _factory,  # type: ignore[arg-type]
        workspace_id=uuid.uuid4(),
        embedding_model="m",
    )
    await asyncio.gather(*(backend.search([0.1, 0.2]) for _ in range(4)))

    assert len(opened) == 4, "a session per call — not one shared across chunks"
    assert len({id(s) for s in opened}) == 4
    assert peak > 1, "the calls did not actually overlap; the test proves nothing"
