"""B1 (graph 절반) — 도달하지 않는 그래프 백엔드를 지운다. 실측 기준.

SoT: ``~/Docs/BSVibe_B1_Graph_Island_Measurement_2026-08-21.md``.
#792 로 retrieval 쪽 7모듈이 나간 뒤 다시 잰 결과: 섬 53 · 문 24 · 도달 40 ·
**미도달 9 (2,854 LOC)**.

## 이 PR 이 실측보다 두 모듈 더 지우는 이유 — import 도달 ≠ 실행 도달

``graph_backend`` 와 ``retrieval.graph_retriever`` 는 **import 로는 도달한다.**
그런데 실행으로는 닿지 않는다:

* ``GraphBackend`` ABC 의 구현체는 ``VaultBackend`` 와 ``GraphStore`` **둘뿐**이고
  둘 다 위 9모듈에 들어 있다 → 지우면 **구현체 0개인 ABC** 가 남는다.
* ``GraphRetriever`` 를 프로덕션이 생성하는 곳이 **0곳**이다.
  ``VaultRetriever(graph_retriever=…)`` 를 넘기는 호출자가 없어
  ``self._graph_retriever`` 는 **항상 None** — 그래프 분기 두 개가 죽은 분기다.

∴ 9모듈만 지우면 구현 없는 ABC + 절대 안 타는 분기가 남는다. 함께 지운다.

## ``test_compile_batch_e2e`` 는 지우지 않는다 — 한 단계만 들어낸다

그 파일은 **살아 있는** ``IngestCompiler.compile_batch`` E2E 다. 다만 스스로
``GraphExtractor`` 를 돌리고 그 결과를 검증했는데, **프로덕션 컴파일 경로는
``GraphExtractor`` 를 한 번도 호출하지 않는다** (호출자 0). 자기가 넣어준 값을
자기가 확인하는 단계였다. 그 단계만 들어내고 검색 검증은 남긴다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_DEAD_MODULES = (
    "backend.knowledge.graph.analytics",
    "backend.knowledge.graph.edge_lifecycle",
    "backend.knowledge.graph.graph_backend",
    "backend.knowledge.graph.graph_extractor",
    "backend.knowledge.graph.graph_store",
    "backend.knowledge.graph.graph_subscriber",
    "backend.knowledge.graph.index_subscriber",
    "backend.knowledge.graph.vault_backend",
    "backend.knowledge.graph.vault_linter",
    "backend.knowledge.graph.write_queue",
    "backend.knowledge.retrieval.graph_retriever",
)

# 삭제 전후 모두 살아 있어야 하는 것.
_LIVE_MODULES = (
    "backend.knowledge.graph.vault",
    "backend.knowledge.graph.storage",
    "backend.knowledge.graph.writer",
    "backend.knowledge.graph.writer_tools",
    "backend.knowledge.graph.markdown_utils",
    "backend.knowledge.graph.graph_models",
    "backend.knowledge.graph.note",
    "backend.knowledge.graph.restricted",
    "backend.knowledge.graph.sync",
    "backend.knowledge.retrieval.retriever",
    "backend.knowledge.retrieval.ontology",
    "backend.knowledge.retrieval.ingest_retriever",
    "backend.knowledge.retrieval.storage.pg",
    "backend.knowledge.code_graph.graph",
)

_ROOT = Path(__file__).resolve().parents[3]
_TREES = (_ROOT / "backend", _ROOT / "tests")


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_unreached_graph_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_source_still_points_at_the_deleted_modules() -> None:
    """문자열 import 와 docstring 의 dangling 참조까지 잡는다 (`backend/` + `tests/`)."""
    needles = tuple(m.removeprefix("backend.") for m in _DEAD_MODULES)
    offenders = [
        f"{path.relative_to(_ROOT)}:{i}"
        for tree in _TREES
        for path in tree.rglob("*.py")
        if path != Path(__file__)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(needle in line for needle in needles)
    ]
    assert not offenders, f"삭제된 모듈을 아직 가리킨다: {offenders}"


def test_the_retriever_no_longer_carries_a_branch_that_never_runs() -> None:
    """``graph_retriever`` 파라미터가 사라진다 — 넘기는 프로덕션 호출자가 0곳이었다."""
    import inspect

    from backend.knowledge.retrieval.retriever import VaultRetriever

    assert "graph_retriever" not in inspect.signature(VaultRetriever.__init__).parameters


@pytest.mark.parametrize("module", _LIVE_MODULES)
def test_the_reached_half_survives(module: str) -> None:
    """양성 대조군 — vault(FS) 쓰기·읽기 경로와 살아 있는 MCP 그래프 구현."""
    importlib.import_module(module)


def test_the_live_mcp_graph_tools_still_resolve() -> None:
    """양성 대조군 — ``bsvibe_graph_*`` 은 ``code_graph`` 를 쓴다. 이름만 비슷한 다른 패키지다."""
    tools = importlib.import_module("backend.mcp.tools.graph_tools")
    assert hasattr(tools, "register_graph_tools")


def test_the_compiler_vector_backend_survives() -> None:
    """양성 대조군 — ``PgNoteVectorBackend`` 는 #784 가 컴파일러에게 붙여준 백엔드다."""
    pg = importlib.import_module("backend.knowledge.retrieval.storage.pg")
    assert hasattr(pg, "PgNoteVectorBackend")
