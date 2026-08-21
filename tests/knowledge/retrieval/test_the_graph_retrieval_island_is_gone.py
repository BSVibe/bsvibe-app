"""B1 (retrieval 절반) — 바깥에서 전이적으로도 닿지 않는 검색 모듈을 지운다.

감사 B1 은 *"엔티티/관계 그래프 백엔드 전체가 프로덕션 미도달 섬 (24모듈 · ~5,850 LOC)"*
이라고 적었다. **실측이 그것을 두 군데 정정했다** — SoT
``~/Docs/BSVibe_B1_Graph_Island_Measurement_2026-08-21.md``:

1. 살아 있는 MCP 툴 ``bsvibe_graph_*`` 은 ``backend.knowledge.**code_graph**`` 를 쓴다.
   B1 이 지목한 ``knowledge.graph`` 가 **아니다**.
2. ``knowledge/{graph,retrieval}`` 은 섬이 아니다 — ``backend/knowledge/`` **바깥의
   24개 모듈**이 직접 import 한다. AST 로 import 그래프를 세우고 그 문(door)에서
   전이 폐포를 돌리면 도달 40 · **미도달 20 (4,069 LOC)** 이다.

이 PR 은 그중 **retrieval 쪽 7모듈 (1,173 LOC)** 만 지운다. 의존 방향이 한쪽
(retrieval → graph) 이라 graph 쪽보다 먼저 나갈 수 있다.

⚠️ **``retrieval.storage.memory`` 는 지우지 않는다.** 전이 폐포는 그것을 미도달로
표시했지만 **``tests/`` 를 그래프에 안 넣은 사각지대**였다 — 테스트 4개가 직접
import 하는 **살아 있는 테스트 더블**이다. 죽은 프로덕션 코드를 시험하는 알리바이
테스트와, 살아 있는 코드를 시험하기 위한 테스트 인프라는 다르다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_DEAD_MODULES = (
    "backend.knowledge.retrieval.community",
    "backend.knowledge.retrieval.conflict",
    "backend.knowledge.retrieval.contradiction",
    "backend.knowledge.retrieval.dedup",
    "backend.knowledge.retrieval.hybrid_search",
    "backend.knowledge.retrieval.multimodal",
    "backend.knowledge.retrieval.review_queue",
)

# 삭제 전후 모두 살아 있어야 하는 것 — 감사가 삭제 대상으로 적었던 둘을 포함한다.
_LIVE_MODULES = (
    "backend.knowledge.retrieval.ontology",
    "backend.knowledge.retrieval.graph_retriever",
    "backend.knowledge.retrieval.answer_grounding",
    "backend.knowledge.retrieval.ingest_retriever",
    "backend.knowledge.retrieval.embedder_resolution",
    "backend.knowledge.retrieval.reconcile",
    "backend.knowledge.retrieval.resolved_decisions_retriever",
    "backend.knowledge.retrieval.decision_note_locator",
    "backend.knowledge.retrieval.semantic_note_retriever",
    "backend.knowledge.retrieval.knowledge_item",
    "backend.knowledge.graph.graph_backend",
)

_ROOT = Path(__file__).resolve().parents[3]
_TREES = (_ROOT / "backend", _ROOT / "tests")


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_unreached_retrieval_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_no_source_still_points_at_the_deleted_modules() -> None:
    """문자열 import 와 docstring 의 dangling 참조까지 잡는다.

    ``backend/`` 만 봐서는 부족하다 — #791 에서 세 번째 알리바이가
    ``importlib.import_module("…")`` 라는 **문자열**이라 import 문 grep 이 못 봤다."""
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


@pytest.mark.parametrize("module", _LIVE_MODULES)
def test_the_reached_half_survives(module: str) -> None:
    """양성 대조군 — 감사는 ``ontology`` 와 ``graph_backend`` 도 삭제 대상으로 적었다.

    실측으로는 둘 다 **도달한다**. 사라지면 지식 검색이 죽는다."""
    importlib.import_module(module)


def test_the_in_memory_test_double_survives() -> None:
    """양성 대조군 — 전이 폐포가 미도달로 표시했지만 테스트 4개가 쓰는 살아 있는 더블이다."""
    storage = importlib.import_module("backend.knowledge.retrieval.storage")
    assert hasattr(storage, "InMemoryNoteVectorBackend")
    assert hasattr(storage, "PgNoteVectorBackend"), "#784 가 컴파일러에게 붙여준 백엔드"


def test_the_compiler_vector_backend_survives() -> None:
    """양성 대조군 — ``PgNoteVectorBackend`` 는 #784 의 대상이다 (prod 1,714행)."""
    pg = importlib.import_module("backend.knowledge.retrieval.storage.pg")
    assert hasattr(pg, "PgNoteVectorBackend")
