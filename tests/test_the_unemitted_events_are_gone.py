"""감사 B3 — 아무도 발화하지 않는 이벤트 종류와, 아무도 만들지 않는 구독자.

## ⚠️ 이 발견은 세 번 좁혀졌다

**감사 주장**: *"``EventType`` 37개 중 28개 발화·구독 0, 구독자 클래스 4개는 버스 미등록"*

1. 실측하니 멤버는 **36개**, 완전 미사용은 **20개**다.
2. **6개는 enum 이 아니라 문자열 값으로 발화한다** (``"note_updated"`` 같은 형태) —
   enum 이름만 grep 했으면 그 여섯을 죽은 것으로 오판했다. 그중 하나가
   ``INGEST_COMPILE_BATCH_COMPLETE`` 로, 컴파일러 관측의 핵심 지표다.
3. 구독자 클래스는 4개가 아니라 **2개** 남았다 (graph/index 쪽 둘은 #793 에서
   섬과 함께 사라졌다).

## ⚠️ 버스는 살아 있다 — 내가 과대 주장할 뻔했다

한때 *"버스 전체가 소비자 없음"* 으로 읽었는데 **틀렸다.** ``emit_event`` 를
ingest 컴파일러 · GardenWriter · canonicalization apply 파이프라인이 부르고,
``plugin/audit`` 의 구독자가 플러그인 import 시점에 등록된다. 살아 있는 배관이다.

죽은 것은 **그 배관을 타지 않는 어휘**뿐이다.

## 지우는 것

* 발화 0 + 구독 0 인 ``EventType`` 멤버 **20개**
* ``CanonicalizationIndexSubscriber`` — 모듈째 import 하는 곳 **0**
* ``VectorSubscriber`` **클래스** — 산문 4곳에서만 언급되고 인스턴스화 0.
  ⚠️ 같은 모듈의 ``embed_and_store_note`` / ``_DEFAULT_MAX_EMBED_CHARS`` 는
  ``reconcile.py`` 가 실제로 쓴다 — **모듈은 남기고 클래스만** 지운다.
"""

from __future__ import annotations

import importlib

import pytest

_DEAD_MEMBERS = (
    "PLUGIN_RUN_START",
    "PLUGIN_RUN_COMPLETE",
    "PLUGIN_RUN_ERROR",
    "SKILL_RUN_START",
    "SKILL_GATHER_COMPLETE",
    "SKILL_LLM_RESPONSE",
    "SKILL_APPLY_COMPLETE",
    "SKILL_RUN_COMPLETE",
    "SKILL_RUN_ERROR",
    "TRIGGER_FIRED",
    "TOOL_CALL_START",
    "TOOL_CALL_COMPLETE",
    "INPUT_RECEIVED",
    "INPUT_COMPLETE",
    "INGEST_COMPILE_START",
    "INGEST_COMPILE_COMPLETE",
    "CREDENTIAL_SETUP_REQUIRED",
    "CANONICALIZATION_PROPOSAL_CREATED",
    "CANONICALIZATION_POLICY_UPDATED",
    "CANONICALIZATION_POLICY_CONFLICT",
)

#: **문자열 값으로 발화되는** 멤버 — enum grep 만으로는 안 보인다.
_EMITTED_AS_STRINGS = (
    "SEED_WRITTEN",
    "GARDEN_WRITTEN",
    "ACTION_LOGGED",
    "NOTE_UPDATED",
    "NOTE_DELETED",
    "INGEST_COMPILE_BATCH_COMPLETE",
)


@pytest.mark.parametrize("member", _DEAD_MEMBERS)
def test_the_unemitted_event_type_is_gone(member: str) -> None:
    from backend.knowledge._internal.events import EventType

    assert not hasattr(EventType, member), f"{member} 이 아직 있다"


@pytest.mark.parametrize("member", _EMITTED_AS_STRINGS)
def test_the_string_emitted_members_survive(member: str) -> None:
    """양성 대조군 — **이 여섯은 enum 이름으로 grep 하면 안 보인다.**

    ``INGEST_COMPILE_BATCH_COMPLETE`` 는 컴파일러 관측의 핵심 지표다."""
    from backend.knowledge._internal.events import EventType

    assert hasattr(EventType, member), f"문자열로 발화되는 {member} 를 지웠다"


def test_the_never_instantiated_canon_subscriber_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.knowledge.canonicalization.index_subscriber")


def test_the_never_instantiated_vector_subscriber_class_is_gone() -> None:
    mod = importlib.import_module("backend.knowledge.retrieval.vector_subscriber")
    assert not hasattr(mod, "VectorSubscriber")


def test_the_vector_helpers_that_reconcile_uses_survive() -> None:
    """양성 대조군 — **모듈은 남는다.** ``reconcile`` 이 이 둘을 import 한다."""
    mod = importlib.import_module("backend.knowledge.retrieval.vector_subscriber")
    assert hasattr(mod, "embed_and_store_note")
    assert hasattr(mod, "_DEFAULT_MAX_EMBED_CHARS")

    reconcile = importlib.import_module("backend.knowledge.retrieval.reconcile")
    assert hasattr(reconcile, "reconcile_embeddings")


def test_the_bus_and_its_live_producers_survive() -> None:
    """양성 대조군 — **버스는 살아 있다.** 컴파일러·writer 가 실제로 발화한다."""
    events = importlib.import_module("backend.knowledge._internal.events")
    assert hasattr(events, "emit_event")

    compiler = importlib.import_module("backend.knowledge.ingest.ingest_compiler._compiler")
    assert "emit_event" in importlib.import_module("inspect").getsource(compiler)
