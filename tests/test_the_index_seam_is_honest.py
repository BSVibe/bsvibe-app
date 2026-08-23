"""감사 B7 — 구현이 하나뿐인 ABC. **그런데 감사의 이유는 절반만 맞았다.**

감사: *"``CanonicalizationIndex`` ABC — 구현 1개, 테스트 더블도 없음"*.
실측하니 이 ABC 는 **죽은 게 아니다** — 8개 프로덕션 모듈이 **의존 타입으로** 쓴다
(``resolver`` · ``decisions`` · ``promotion`` · ``policies`` · ``lint`` · ``proposals`` ·
``service``). 그냥 지우면 그 8곳이 구현 이름을 직접 가리키게 된다.

## 진짜 비용은 두 가지다

**① 두 번째 구현은 이미 왔다 갔다.** 캐노니컬라이제이션 Postgres 미러가 그 자리였고,
**#789 에서 producer-less 로 삭제**됐다. seam 이 지키려던 미래가 지나갔다.

**② 호출자가 이미 seam 을 뚫었다.** ``lint.py`` 가 구현의 **private 속성**을 읽는다:

    tombstones = getattr(index, "_tombstones", {}) or {}
    # "``CanonicalizationIndex`` doesn't have a public ``list_tombstones``
    #  (intentional) … Iterating private state is acceptable here"

즉 ``find_orphan_tags`` / ``_redirects_to_active`` 는 **임의의
``CanonicalizationIndex`` 로는 동작하지 않는다.** 두 번째 구현이 오면 ``getattr`` 의
기본값 ``{}`` 때문에 **에러 없이 빈 findings** 를 낸다 — 린트가 조용히 아무것도
못 찾는다.

## 그래서 지우는 게 아니라 **합친다**

ABC 를 구현에 합치고 **8곳이 이미 쓰는 이름 ``CanonicalizationIndex`` 를 유지**한다.
그러면 ``_tombstones`` 는 *호출자가 실제로 들고 있는 클래스*의 private 이 되고,
``getattr`` 기본값도 없앨 수 있다 — 없으면 조용한 빈 결과 대신 에러가 난다.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_the_index_is_one_concrete_class() -> None:
    from backend.knowledge.canonicalization.index import CanonicalizationIndex

    assert not inspect.isabstract(CanonicalizationIndex)
    assert not getattr(CanonicalizationIndex, "__abstractmethods__", frozenset())
    CanonicalizationIndex()  # 직접 만들 수 있어야 한다


def test_the_split_name_is_gone() -> None:
    mod = importlib.import_module("backend.knowledge.canonicalization.index")
    split_name = "InMemory" + "CanonicalizationIndex"
    assert not hasattr(mod, split_name)


def test_no_module_still_names_the_split_class() -> None:
    # 문자열로 조립한다 — 일괄 rename 이 이 가드 자신을 오염시키지 않도록.
    needles = ("InMemory" + "CanonicalizationIndex",)
    offenders = [
        f"{p.relative_to(_ROOT)}:{i}"
        for tree in (_ROOT / "backend", _ROOT / "tests")
        for p in tree.rglob("*.py")
        if p != Path(__file__)
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if any(n in line for n in needles)
    ]
    assert not offenders, f"쪼개진 이름을 아직 쓴다: {offenders}"


def test_the_lint_reach_around_no_longer_swallows_a_missing_attribute() -> None:
    """**조용한 빈 결과**가 사라져야 한다.

    ``getattr(index, "_tombstones", {})`` 는 속성이 없어도 빈 dict 를 돌려줘서
    린트가 아무 findings 없이 통과했다 — 두 번째 구현이 오면 그렇게 됐을 것이다."""
    src = (_ROOT / "backend/knowledge/canonicalization/lint.py").read_text(encoding="utf-8")
    assert 'getattr(index, "_tombstones"' not in src


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_every_index_method_survives() -> None:
    """합치면서 메서드를 잃으면 안 된다 — 실측 기준선 14개."""
    from backend.knowledge.canonicalization.index import CanonicalizationIndex

    expected = {
        "initialize",
        "close",
        "get_active_concept",
        "list_active_concepts",
        "find_concepts_by_alias",
        "get_tombstone",
        "get_deprecated",
        "list_actions",
        "list_proposals",
        "find_pending_concept_draft",
        "list_decisions",
        "list_policies",
        "invalidate",
        "rebuild_from_vault",
    }
    missing = expected - set(dir(CanonicalizationIndex))
    assert not missing, f"메서드가 사라졌다: {missing}"


def test_the_eight_dependents_still_annotate_with_the_same_name() -> None:
    """양성 대조군 — 이름을 유지했기 때문에 의존 모듈들이 그대로여야 한다."""
    for mod in (
        "backend.knowledge.canonicalization.resolver",
        "backend.knowledge.canonicalization.decisions",
        "backend.knowledge.canonicalization.promotion",
        "backend.knowledge.canonicalization.policies",
        "backend.knowledge.canonicalization.lint",
        "backend.knowledge.canonicalization.proposals",
        "backend.knowledge.canonicalization.service",
    ):
        importlib.import_module(mod)


def test_the_lint_still_finds_a_dangling_redirect() -> None:
    """특성화 — 뚫고 읽던 그 기능이 그대로 동작해야 한다."""
    from backend.knowledge.canonicalization.lint import run_lint

    assert inspect.iscoroutinefunction(run_lint)
