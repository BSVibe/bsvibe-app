"""검색이 한국어를 본다 (트랙 A-0).

형님은 한국어로 가르친다. 그런데 정밀 검색기 세 개가 전부 토크나이저로
``[a-z0-9]+`` 를 쓴다 — ASCII 전용이다. prod 컨테이너에서 실제로 포착된
형님 교정 문장을 넣어보면 양쪽 토큰 집합이 ``set()`` 이고, 겹침 판정
(``resolved_decisions_retriever.py:236``) 이 ``continue`` 하므로 **그 교정은
영원히 표면화되지 않는다.**

ratchet(§5)은 "한 번 감독된 실수를 미래 verify 가 retrieve 한다"에 걸려 있다.
retrieve 가 한국어를 못 읽으면 ratchet 은 형님 언어로는 존재하지 않는다.

설계 판단 (`BSVibe_Trust_Ratchet_Restoration_Design.md` §5 A-0):

* **문자 bigram** — 형태소 분석기는 시스템 의존(mecab)이라 배제. 공백 분절은
  조사 때문에 실패한다(``명세는`` / ``명세를`` 가 다른 토큰). bigram 은 ``명세``
  가 셋 다에서 나오므로 그 문제를 자연히 푼다.
* **정밀도 가드** — bigram 은 우연 겹침이 늘어난다. 그래서 ASCII 처럼 "겹침이
  하나라도 있으면 통과"로 두지 않는다.
* **ASCII 회귀 0** — 각 검색기의 기존 파라미터(min_len / stopwords)는 그대로.
  CJK 문법만 공유한다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.config import get_settings
from backend.knowledge.factory import KnowledgeFactory
from backend.knowledge.graph.vault import Vault
from backend.knowledge.graph.writer import GardenNote
from backend.knowledge.graph.writer_core import GardenWriter
from backend.knowledge.retrieval.signal_tokens import overlaps, tokenize

pytestmark = pytest.mark.asyncio

_REGION = "us-1"

#: prod 에서 실제로 포착된 형님 교정 (garden/seedling/settle-decision-resolved-…).
_REAL_CORRECTION = (
    "명세는 끝났다. 더 고치지 마라. 문서를 한 줄이라도 더 손보면 그건 이 턴의 실패다. "
    "이제 그 명세대로 구현해라. 이 턴의 산출물은 문서가 아니라 실제로 돌아가는 검사다."
)


# ── 토크나이저 ───────────────────────────────────────────────────────────────


def test_korean_text_produces_tokens_at_all() -> None:
    """지금은 ``set()`` 이다. 그것이 이 트랙의 출발점."""
    assert tokenize("명세는 끝났다") != set()


def test_a_particle_does_not_hide_the_stem() -> None:
    """조사가 붙어도 같은 어간이 잡혀야 한다 — 공백 분절을 버린 이유."""
    assert tokenize("명세는") & tokenize("명세를")
    assert tokenize("검증이 필요하다") & tokenize("검증을 돌려라")


def test_ascii_behaviour_is_unchanged() -> None:
    """회귀 0 — 기존 ASCII 문법은 그대로."""
    assert tokenize("backend/workflow/honesty.py", min_len=3) == {"backend", "workflow", "honesty"}
    assert "py" not in tokenize("honesty.py", min_len=3)
    assert "py" in tokenize("honesty.py", min_len=2)


def test_stopwords_still_strip() -> None:
    assert tokenize("the and for", min_len=3, stopwords=frozenset({"the", "and", "for"})) == set()


def test_mixed_korean_and_code_keeps_both() -> None:
    toks = tokenize("honesty.py 의 등급 설명", min_len=3)
    assert "honesty" in toks
    assert tokenize("등급") <= toks


# ── 정밀도 가드 (bigram 은 느슨해지기 쉽다) ─────────────────────────────────


def test_a_related_korean_signal_overlaps() -> None:
    assert overlaps(_REAL_CORRECTION, "명세대로 구현하고 검사를 돌려라")


def test_an_unrelated_korean_signal_does_not_overlap() -> None:
    """음성 대조 — 이것이 A-0 의 수락 기준이다. 한국어를 보이게 만들면서
    무관한 것까지 끌어오면 `df66a253` 를 한국어로 재현하는 것이다."""
    assert not overlaps(_REAL_CORRECTION, "텔레그램 알림 문구를 친절하게 바꿔줘")
    assert not overlaps(_REAL_CORRECTION, "결제 웹훅 서명을 검증하는 로직 추가")


def test_one_incidental_bigram_is_not_enough() -> None:
    """단일 우연 겹침으로는 통과하지 못한다 — ASCII 의 '겹치면 통과'를 CJK 에
    그대로 옮기면 ``추가`` 같은 범용어 하나로 무관한 노트가 딸려온다.

    (반대로 ``데이터베이스`` ↔ ``데이터`` 처럼 진짜 어간을 공유하면 bigram 이
    2개 이상 겹치므로 통과한다 — 그것이 이 하한을 1이 아니라 2로 둔 이유다.)"""
    assert not overlaps("등급 설명 추가", "추가 요금 정책")
    assert overlaps("데이터베이스 마이그레이션", "데이터 정합성 리포트")


def test_ascii_single_token_overlap_still_passes() -> None:
    """ASCII 쪽 판정은 건드리지 않는다(회귀 0)."""
    assert overlaps("touch backend/honesty.py", "backend rewrite")


# ── 검색기 끝단 (실제 vault 상태) ────────────────────────────────────────────


async def _seed(vault_root: Path, workspace_id: str, *, question: str, answer: str) -> None:
    ws_root = vault_root / get_settings().knowledge_default_region / workspace_id
    ws_root.mkdir(parents=True, exist_ok=True)
    writer = GardenWriter(vault=Vault(ws_root))
    summary = f"Decision resolved — Q: {question} A: {answer}"
    await writer.write_garden(
        GardenNote(
            title=f"Settle: {summary[:80]}",
            content=summary,
            source="settle_worker",
            knowledge_layer="episodic",
            tags=["settle", "verified-run", "decision-resolution"],
            extra_fields={
                "kind": "decision_resolution",
                "question": question,
                "answer": answer,
                "intent_text": None,
                "resolved_at": datetime.now(tz=UTC).isoformat(),
            },
        )
    )


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "vault"


@pytest.fixture
def workspace_id() -> str:
    return str(uuid.uuid4())


async def test_a_korean_correction_reaches_a_future_run(
    vault_root: Path, workspace_id: str
) -> None:
    """§5 의 문장 그대로: 한 번 감독된 실수를 미래 verify 가 retrieve 한다."""
    await _seed(
        vault_root,
        workspace_id,
        question="이 작업은 검증됨으로 표시하기 전에 검토가 필요해요",
        answer=_REAL_CORRECTION,
    )
    retriever = KnowledgeFactory(workspace_id=workspace_id, vault_root=vault_root).retriever()

    statements = await retriever.retrieve_for_signals("명세대로 구현하고 검사를 돌려라")

    assert statements, "한국어 교정이 관련 신호에서 표면화되어야 한다"
    assert "명세" in "\n".join(statements)


async def test_an_unrelated_korean_run_pulls_nothing(vault_root: Path, workspace_id: str) -> None:
    """무관한 작업은 아무것도 끌어오지 않는다 — 음성 대조의 끝단 버전."""
    await _seed(
        vault_root,
        workspace_id,
        question="이 작업은 검증됨으로 표시하기 전에 검토가 필요해요",
        answer=_REAL_CORRECTION,
    )
    retriever = KnowledgeFactory(workspace_id=workspace_id, vault_root=vault_root).retriever()

    assert await retriever.retrieve_for_signals("텔레그램 알림 문구를 친절하게 바꿔줘") == []
