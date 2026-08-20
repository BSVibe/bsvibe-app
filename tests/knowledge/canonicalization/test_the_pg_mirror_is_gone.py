"""캐노니컬라이제이션 Postgres 미러를 지운다 — 코드베이스가 스스로 자백했다.

형님 판정 2026-08-20. 이건 감사자의 판단이 아니라 **코드의 자백**이다:

``backend/api/v1/decisions/__init__.py``
    *"proposals are markdown notes in the vault, NOT rows in the (currently
    **producer-less**) ``canonicalization_proposals`` DB table."*

``backend/api/v1/workspace_compliance.py``
    *"It is emphatically NOT the ``canonical_anchors`` DB table, which is
    **producer-less: nothing writes it**, so reading it under-reported the
    founder's knowledge as empty for every workspace (a **GDPR Art. 15/20
    defect**)."*

→ **이 미러는 이미 실제 규제 결함을 일으켰다.** 진짜 SoT 는 볼트 파일이고
(``canonicalization/{decisions,policies,proposals}.py``), DB 쪽은 그 위에 얹힌
두 번째 표현이었다.

prod 실측 (2026-08-20) — 다섯 테이블 전부 **0행**:
``canonical_anchors`` · ``canonicalization_proposals`` ·
``canonicalization_decisions`` · ``canonicalization_policies`` ·
``retrieval_queries``.

Repository 계층도 같다. ``NoteRepository`` 는 자기 docstring 에
*"Adding a real caller justifies adding a method; never speculatively"* 라고
적어놓고 **클래스 자체가 speculative** 했다 — 프로덕션 호출자 0.

⚠️ ``NoteEmbeddingRow`` / ``note_embeddings`` 는 **살아 있다**. pgvector 임베딩을
담고 ``PgNoteVectorBackend`` 가 쓰며 prod 에 1,700행 넘게 있다. 같은 파일에 살 뿐
이 미러와 무관하고, 아래가 그것을 못박는다.
"""

from __future__ import annotations

import importlib

import pytest

_DEAD_MODULES = (
    "backend.knowledge.canonicalization.db",
    "backend.knowledge.domain.repositories",
    "backend.knowledge.infrastructure.repositories",
    "backend.api.v1._knowledge_deps",
)


@pytest.mark.parametrize("module", _DEAD_MODULES)
def test_the_producerless_mirror_module_is_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_the_retrieval_query_row_is_gone() -> None:
    """``retrieval_queries`` 도 writer·reader 0 이었다."""
    db = importlib.import_module("backend.knowledge.retrieval.db")
    assert not hasattr(db, "RetrievalQuery")


def test_the_note_embedding_row_survives() -> None:
    """양성 대조군 — 같은 파일에 사는 살아 있는 테이블까지 지우면 시맨틱 검색이 죽는다.

    ``PgNoteVectorBackend`` 가 이 행을 쓰고, #784 가 컴파일러에게 붙여준 것이
    바로 그 백엔드다."""
    db = importlib.import_module("backend.knowledge.retrieval.db")
    assert db.NoteEmbeddingRow.__tablename__ == "note_embeddings"

    backend_mod = importlib.import_module("backend.knowledge.retrieval.storage.pg")
    assert hasattr(backend_mod, "PgNoteVectorBackend")


def test_the_vault_remains_the_source_of_truth() -> None:
    """볼트 쪽 캐노니컬라이제이션은 현역이다 — 이 삭제의 대상이 아니다."""
    for module in ("decisions", "policies", "proposals", "paths"):
        importlib.import_module(f"backend.knowledge.canonicalization.{module}")
