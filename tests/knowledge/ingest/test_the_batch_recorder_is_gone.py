"""``ingest_batches`` 를 지운다 — producer 가 붙은 적 없는 두 번째 표현.

형님 판정 2026-08-20. 실측이 셋 다 0 이었다:

===========================================  ===
``ingest_batches`` 행 수                       0
``IngestBatchRecorder`` 프로덕션 구현체          0
``batch_recorder=`` 를 넘기는 생성 지점          0
===========================================  ===

``retriever=`` (#784) 와 **똑같은 결함이 바로 옆 인자에** 있었다. 다만 그쪽은 넘길
자산(``note_embeddings`` 1,714행)이 살아 있었고 이쪽은 없다 — 구조 감사가 지목한
지배적 실패 모드(*"producer 가 붙은 적 없는 두 번째 표현"*, 전체의 ~70%)의 정확한
모양이라, 답은 배선이 아니라 삭제다.

**삭제해도 관측을 잃지 않는다는 것을 먼저 확인했다.** 살아 있는 대체 지표
``ingest_compile_batch_complete`` 로그는 로컬 변수로만 만들어지고
``IngestBatchRecord`` 를 거치지 않는다. 그래서 그 로그는 이 삭제 뒤에도 남고, 아래
테스트가 그것을 못박는다 — 우리가 만드는 건 인프라이고, 에이전트를 눈멀게 하는
삭제는 치명적이다.

표류의 증거도 남긴다: 테이블 컬럼(``seed_count``/``decisions``/``model_used``)과
``IngestBatchRecord`` 필드(``seed_source``/``notes_created``/``llm_calls``/…)는
서로 맞지도 않았다. 한 번도 함께 돈 적이 없다는 뜻이다.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


def test_the_analytics_table_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.knowledge" + ".ingest.db")


def test_the_recorder_seam_is_gone() -> None:
    """Protocol 과 레코드 타입 둘 다 — 선언만 있고 구현체가 없던 것들."""
    actions = importlib.import_module("backend.knowledge.ingest.ingest_compiler._actions")
    package = importlib.import_module("backend.knowledge.ingest.ingest_compiler")

    for name in ("IngestBatchRecord", "IngestBatchRecorder"):
        assert not hasattr(actions, name), f"{name} 이 아직 있다"
        assert not hasattr(package, name), f"{name} 이 아직 export 된다"
        assert name not in getattr(package, "__all__", ())


def test_the_compiler_no_longer_takes_a_recorder() -> None:
    """인자가 남아 있으면 아무도 안 넘기는 죽은 seam 이 그대로 남는다."""
    from backend.knowledge.ingest.ingest_compiler import IngestCompiler

    assert "batch_recorder" not in inspect.signature(IngestCompiler.__init__).parameters


def test_the_surviving_observation_still_fires() -> None:
    """삭제가 관측을 함께 지우지 않았다는 증거.

    ``ingest_compile_batch_complete`` 는 컴파일이 몇 번 돌았고 몇 개를 update 했는지
    묻는 **유일하게 살아 있는** 지표다. 이 삭제의 전제가 그것이므로, 전제를 테스트로
    고정한다."""
    from backend.knowledge.ingest.ingest_compiler import _compiler

    source = inspect.getsource(_compiler)
    assert '"ingest_compile_batch_complete"' in source
    for field in ("source=", "updated=", "created=", "llm_calls=", "chunk_failures="):
        assert field in source, f"{field} 가 로그에서 사라졌다 — 관측을 잃었다"
