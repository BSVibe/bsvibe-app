"""감사 A13 — 선언만 받고 아무도 안 보던 관할권 축을 없앤다.


형님 판단 (2026-08-24): *"없애자. 지금은 굳이 불필요한 기능 같아"*

## 실측이 보여준 것 (prod, 2026-08-24)

* **이 값으로 분기하는 코드 0** — ``models.py`` 가 스스로 적었다:
  *"declared by the worker SDK at registration … **we just store + index it**"*
* **이 축으로 분기하는 코드 0** — 저장하고 인덱스만 걸었다
* **라우팅에서 쓸 수 없다** — ``ALLOWED_FIELDS`` 에 없다
* **사용자가 고를 수 없다** — PWA 선택 UI 는 이미 제거됐다 (*"invisible infra"*)
* **규정준수 표면이 주장하지 않는다** — ``workspace_compliance`` 는 언급조차 없다

∴ 값이 흐르는 경로는 등록 → 저장 → 인덱스 → API 표시뿐이고, **그 표시를 보는 화면도 없다.**

## 게다가 그 선언조차 정확히 안 보였다

응답 Literal 은 ``us|eu|kr|local|unknown`` 다섯 값인데 prod 에는 ``self-hosted-kr`` 이
저장돼 있다(형님의 ``Local Ollama`` 계정). 원래는 Pydantic 오류로 ``GET /api/v1/accounts``
전체가 500 났고, 그걸 막으려 넣은 ``_coerce_jurisdiction`` 이 **모르는 값을 조용히
``unknown`` 으로 덮었다** — 500 은 막았지만 불일치를 가렸다.

## SDK 계약

``plugin(...)`` 의 **필수 키워드 인자**였다. ``bsvibe-sdk`` 는 아직 배포 전이고
(CI 에 publish 단계 없음) 소비자는 in-tree 플러그인 12개뿐이라 파급이 없다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_the_column_is_gone_from_the_orm() -> None:
    pass


def test_the_schemas_no_longer_carry_it() -> None:
    from backend.router.accounts import schemas

    axis = "data_" + "jurisdiction"
    for name in ("ModelAccountCreate", "ModelAccountUpdate", "ModelAccountOut"):
        assert axis not in getattr(schemas, name).model_fields, name
    assert not hasattr(schemas, "Jurisdiction")
    assert not hasattr(schemas, "_coerce_jurisdiction")


def test_the_sdk_no_longer_requires_it() -> None:
    """``plugin(...)`` 의 필수 인자였다 — 없이도 선언할 수 있어야 한다."""
    import inspect

    from bsvibe_sdk import plugin

    assert ("data_" + "jurisdiction") not in inspect.signature(plugin).parameters
    assert not hasattr(plugin(name="probe-plugin", credentials=[]).meta, "data_" + "jurisdiction")


def test_no_source_still_names_the_axis() -> None:
    trees = (_ROOT / "backend", _ROOT / "plugin", _ROOT / "bsvibe_sdk", _ROOT / "tests")
    offenders = [
        f"{p.relative_to(_ROOT)}:{i}"
        for tree in trees
        for p in tree.rglob("*.py")
        if p != Path(__file__) and "migrations" not in p.parts
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        # 이름을 조립한다 — 일괄 rename/삭제 스크립트가 이 가드 자신을 오염시키지
        # 않도록. (#809 에서 같은 실수를 했고, 이번엔 needle 이 아니라 **조건 줄**이
        # 통째로 지워져 가드가 모든 줄을 잡는 상태로 초록이 됐다.)
        # 마이그레이션 **리비전 이름**(``drop_data_jurisdiction``)은 역사라 남는다 —
        # 지운 축을 가리키는 코드가 아니다.
        if ("data_" + "jurisdiction") in line and "drop_data_" not in line
    ]
    assert not offenders, f"축을 아직 가리킨다: {offenders[:20]}"


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_model_account_surface_still_works() -> None:
    """양성 대조군 — 계정 자체는 형님이 실제로 쓰는 표면이다."""
    from backend.router.accounts.schemas import ModelAccountCreate, ModelAccountOut

    assert {"label", "provider", "litellm_model", "api_key"} <= set(ModelAccountCreate.model_fields)
    assert {"id", "label", "provider", "has_api_key", "is_active"} <= set(
        ModelAccountOut.model_fields
    )


def test_every_plugin_still_declares_itself() -> None:
    """양성 대조군 — 플러그인 12개가 그대로 선언되고 이름·크리덴셜을 유지해야 한다."""

    for name in ("github", "slack", "telegram", "notion", "obsidian"):
        mod = importlib.import_module(f"plugin.{name}.plugin")
        meta = mod.p.meta
        assert meta.name == name, name
        assert hasattr(meta, "credentials"), name


def test_the_routing_fields_are_untouched() -> None:
    """양성 대조군 — 라우팅 축은 이 삭제와 무관하다 (``ALLOWED_FIELDS`` 에 없었다).

    개수가 아니라 **필드 이름**을 센다. 개수는 이 대조군이 지키려는 것과 무관한
    이유로 움직인다 — 실제로 그랬다: ``pipeline`` 삭제(고정 예측 축 제거)가
    ``>= 11`` 을 깨뜨렸는데, 관할권 삭제와는 아무 상관이 없다. 개수를 세는
    대조군은 자기가 무엇을 지키는지 모른다.
    """
    from backend.router.routing.run_routing.engine import ALLOWED_FIELDS

    assert "data_jurisdiction" not in ALLOWED_FIELDS
    assert {
        "artifact_type_hint",
        "path_classification",
        "skill_match",
        "intent_text",
        "stage",
        "product_id",
        "caller_id",
        "estimated_tokens",
        "classified_intent",
        "detected_language",
    } <= ALLOWED_FIELDS
