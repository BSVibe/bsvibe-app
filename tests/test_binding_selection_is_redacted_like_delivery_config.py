"""#1033 — ``selection`` 은 응답 리댁션이 없는 채로 ``delivery_config`` 의 키 공간이 됐다.

#1032 가 바인딩의 ``selection`` 을 **``delivery_config`` 오버라이드**로 만들었다
(``effective_delivery_config`` = ``{**account.delivery_config, **binding.selection}``).
그 순간 두 슬롯은 **같은 키 공간**이 됐는데, 리댁션은 한쪽에만 있었다:

* ``connector_accounts.delivery_config`` → 응답에서 ``public_delivery_config()`` 가
  ``SECRET_DELIVERY_KEYS``(``webhook_secret`` · ``signing_secret`` · ``client_secret``)를 **드롭**한다
* ``resource_bindings.selection`` → **그대로 나간다**

즉 ``delivery_config`` 에 넣으면 가려지는 키를 **``selection`` 에 넣으면 그대로 보이고**,
#1032 이후로는 그 키가 **실제로 배송 설정으로 동작**한다 — 넣을 이유가 생겼다.

⭐ **이건 이 레포가 이미 한 번 당한 병이다.** ``connector_redaction.py`` 의 독스트링이
직접 적어 뒀다 — *"미러라서 한 번 어긋났고, 어긋난 결과가 **응답에 라이브 크리덴셜
노출**이었다."* 그때는 REST/MCP 두 미러였고, ``selection`` 이 **세 번째**다.

📏 **실측 (2026-09-22, prod)**: 지금 ``selection`` 세 행에 시크릿은 **없다**. 그래서 이건
유출이 아니라 **#1032 가 방금 만든 초대장**이다. 발견 경위: #1032 배포 후 no-op 을
확인하려고 live DB 행을 프로브했더니 계정 ``delivery_config`` 의 ``webhook_secret`` 이
원본 그대로 찍혔다 — API 응답엔 안 나오는 값이었다.

## 나가는 경로는 셋이고, 셋째가 제일 나쁘다

=================================== ==========================================
``backend/mcp/tools/bindings_tools.py``   ``_row_to_dict`` — MCP 목록
``backend/api/v1/products/_schemas.py``   ``ResourceBindingResponse`` — REST **3개 라우트**가 공유
``backend/api/v1/workspace_compliance.py`` 🚨 **GDPR Art.15/20 export** — 정보주체 내보내기 문서
=================================== ==========================================

GDPR export 가 제일 나쁘다: 시크릿이 **컴플라이언스 산출물**에 실려 나간다.

## 범위 밖 — ``intake.py`` 는 응답이 아니다 (측정하고 뺐다)

``stages/intake.py:209`` 가 ``enriched["selection"]`` 로 dict 를 **통째로** 인바운드
페이로드에 싣는다. 재보니 그건 ``requests.payload``(DB)로 **영속화**되는 축이고
응답 경로가 아니다 — 리댁션이 아니라 *"같은 값이 두 저장소로 복제된다"* 는 **다른 명제**다.
같은 PR 에 섞으면 둘 다 흐려진다. 이슈에 측정 결과만 남긴다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.common.connector_redaction import SECRET_DELIVERY_KEYS

_ROOT = Path(__file__).resolve().parents[1]

#: 시크릿 하나 + 정상 키 하나. **정상 키가 살아남는 것까지** 봐야 리댁터가
#: "전부 지우는" 것이 아님이 증명된다.
_SELECTION_WITH_SECRET: dict[str, Any] = {
    "chat_id": "-100999",
    "webhook_secret": "live-signing-credential",
}


class _FakeBindingRow:
    """``ResourceBindingRow`` 의 응답에 필요한 표면만."""

    def __init__(self, selection: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.product_id = uuid.uuid4()
        self.connector_account_id = uuid.uuid4()
        self.resource_id = "r1"
        self.selection = selection
        self.trigger = {"filters": {}}
        self.output_mode = "safe"
        self.created_at = now
        self.updated_at = now


# ── 호출 지점별로 하나씩 ─────────────────────────────────────────────────
#
# 셋이 **서로 다른 직렬화기**다(dict 리터럴 · pydantic 모델 · dict 리터럴).
# 공유 함수 하나에 가드를 붙여도 세 배선은 증명되지 않는다 — 스킬
# `a-guard-on-the-shared-function-proves-nothing-about-its-call-sites`.


def test_the_mcp_binding_row_drops_the_secret() -> None:
    from backend.mcp.tools.bindings_tools import _row_to_dict

    out = _row_to_dict(_FakeBindingRow(dict(_SELECTION_WITH_SECRET)))  # type: ignore[arg-type]

    assert out["selection"] == {"chat_id": "-100999"}


def test_the_rest_binding_response_drops_the_secret() -> None:
    """``ResourceBindingResponse`` 는 list/create/patch **세 라우트**가 공유하는
    유일한 길목이라, 여기 한 곳이 REST 표면 전부를 덮는다."""
    from backend.api.v1.products._schemas import ResourceBindingResponse

    out = ResourceBindingResponse.model_validate(_FakeBindingRow(dict(_SELECTION_WITH_SECRET)))

    assert out.selection == {"chat_id": "-100999"}


def test_the_gdpr_export_drops_the_secret() -> None:
    """🚨 제일 나쁜 경로 — 시크릿이 정보주체 내보내기 문서에 실린다."""
    from backend.api.v1.workspace_compliance import _binding_export_row

    out = _binding_export_row(_FakeBindingRow(dict(_SELECTION_WITH_SECRET)))  # type: ignore[arg-type]

    assert out["selection"] == {"chat_id": "-100999"}


def test_a_selection_without_secrets_is_untouched() -> None:
    """대조군 — 리댁터가 *전부* 지우는 게 아니라 **시크릿 키만** 지운다."""
    from backend.mcp.tools.bindings_tools import _row_to_dict

    clean = {"chat_id": "8242700007", "artifact_type": "code"}
    out = _row_to_dict(_FakeBindingRow(dict(clean)))  # type: ignore[arg-type]

    assert out["selection"] == clean


def test_every_secret_key_is_dropped_from_selection_too() -> None:
    """키 집합은 ``delivery_config`` 와 **공유**다 — 두 목록이 되면 다시 갈라진다."""
    from backend.mcp.tools.bindings_tools import _row_to_dict

    sel = {k: "x" for k in SECRET_DELIVERY_KEYS} | {"chat_id": "1"}
    out = _row_to_dict(_FakeBindingRow(sel))  # type: ignore[arg-type]

    assert out["selection"] == {"chat_id": "1"}


# ── 전수 가드: 새 표면이 생기면 빨개진다 ──────────────────────────────────
#
# ⚠️ 철자 목록이 아니라 **파일 집합**을 핀으로 박는다 — 스킬
# `absence-guard-listing-spellings-proves-only-imagination`. 새 파일이
# ``selection`` 을 만지면 아래 인구조사가 **모르는 이름**이라 실패하고,
# 그때 리댁트할지 면제할지 **결정을 강제**한다.

_REDACTS = "REDACTS"

#: ``backend/api`` + ``backend/mcp`` 에서 ``selection`` 을 언급하는 파일 **전수**.
#: 값은 *리댁트해야 하는가*, 아니면 *왜 아닌가*.
_SELECTION_SURFACES: dict[str, str] = {
    "backend/api/v1/products/__init__.py": "독스트링만 — 코드 아님",
    "backend/api/v1/products/_schemas.py": _REDACTS,
    "backend/api/v1/products/bindings.py": "입력만 — payload.selection 을 저장소로 넘긴다",
    "backend/api/v1/workers.py": "다른 의미 — 워커 선택(stream selection)",
    "backend/api/v1/workspace_compliance.py": _REDACTS,
    "backend/api/webhooks.py": "독스트링만 — 코드 아님",
    "backend/mcp/tools/bindings_tools.py": _REDACTS,
}


def _census() -> set[str]:
    """``selection`` 을 언급하는 파일을 **런타임이 아니라 트리에서** 센다."""
    found: set[str] = set()
    for d in ("backend/api", "backend/mcp"):
        for p in (_ROOT / d).rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if any("selection" in ln and "sandbox_selection" not in ln for ln in text.splitlines()):
                found.add(str(p.relative_to(_ROOT)))
    return found


def test_the_selection_surface_census_is_pinned() -> None:
    """새 파일이 ``selection`` 을 만지면 여기서 먼저 걸린다."""
    assert _census() == set(_SELECTION_SURFACES), (
        "selection 을 만지는 파일 집합이 바뀌었다. 새 표면이면 리댁트할지 정하고 "
        "_SELECTION_SURFACES 에 이유와 함께 등록해라."
    )


def test_the_census_can_actually_fail() -> None:
    """인구조사 자체의 대조군 — 빈 스캔이면 위 테스트가 **안 돌아서** 통과한다."""
    found = _census()
    assert len(found) >= 7, found
    assert "backend/mcp/tools/bindings_tools.py" in found


def test_every_emitting_surface_calls_the_shared_redactor() -> None:
    """면제가 아닌 파일은 **공유 리댁터를 실제로 부른다.** 자기 목록을 새로 쓰면
    그게 정확히 이 레포가 한 번 당한 미러 드리프트다."""
    for rel, verdict in _SELECTION_SURFACES.items():
        if verdict != _REDACTS:
            continue
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "connector_redaction" in text, f"{rel} 이 공유 리댁터를 import 하지 않는다"
        assert "SECRET_DELIVERY_KEYS = frozenset" not in text, f"{rel} 이 키 목록을 다시 쓴다"


def test_every_exemption_carries_a_reason() -> None:
    """면제에 이유가 없으면 다음 사람이 그게 판단인지 누락인지 모른다."""
    for rel, verdict in _SELECTION_SURFACES.items():
        if verdict == _REDACTS:
            continue
        assert len(verdict) > 10, f"{rel} 의 면제 사유가 비어 있다"
