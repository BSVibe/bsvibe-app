"""canon **제안/액션** 만료 축을 지운다 — 강제된 적 없는 축이다.

감사 **5-9**. 그 문서의 처방은 *"배선하라"* 였지만 재측정이 전제를 무너뜨렸다.

## 실측 (2026-08-30, prod)

| | |
|---|---|
| canon action/proposal 파일 | **1,167** |
| 상태 분포 | `applied` 1,100 · `accepted` 67 |
| ``expire_stale`` 의 대상(`draft`/`pending_approval`/`pending`) | **0건** |
| ``expires_at`` 으로 무언가를 막는 코드 | ``expire_stale`` **자신 외 0** |

배선해도 **0건을 쓸어담는 다섯 번째 폴링 워커**가 생길 뿐이다. 이 저장소는 강제된
적 없는 축을 두 번 다 삭제했다 — 관할권 축(*"강제된 적 없는 축을 지운다"* 는 제목의
마이그레이션)과 ``region``(#845).

.. note::
   앞의 축은 **이름을 적지 않는다.** 그 삭제가 남긴 부재 가드
   (``test_the_unenforced_jurisdiction_axis_is_gone``)는 줄 텍스트를 스캔하므로,
   여기서 인용만 해도 **그 가드가 빨개진다** — 실제로 이 PR 의 첫 게이트에서
   그렇게 떨어졌다. [[absence-guard-listing-spellings-proves-only-imagination]]
   가 말하는 *"산문이 자기 가드를 문다"* 를 내가 그대로 재현한 것이다.

## ⚠️ 내가 한 번 틀렸다 — 이 필드는 **보인다**

처음에 *"canon ``expires_at`` 은 API/MCP 에 안 나간다 → 0"* 이라고 보고했다.
``decisions`` 를 **파일명만 보고** "다른 축"으로 제외한 결과였다. 실제로는
``_proposal_to_dict`` 가 ``models.ProposalEntry`` 를 받고, 그 만료일이 MCP
``bsvibe_decisions_list``/``show`` 와 REST ``GET /api/v1/decisions`` 로 나간다.

그래서 이 삭제는 **BREAKING** 이다. 그리고 그 사실이 삭제를 더 정당화한다 —
아무것도 만료시키지 않는 만료일을 형님께 **보여주는** 것은, 안 보이는 죽은 필드보다
나쁜 거짓말이다. (형님 판단, 2026-08-30)

## 지우지 않는 것 — 이름이 같지만 다른 축이다

``DecisionEntry.expires_at`` 과 ``PolicyEntry.expires_at`` 은 **실제로 강제된다**
(``decisions.py`` · ``policies.py`` 가 ``now >= expires_at`` 으로 판정한다).
철자가 같다고 같은 축이 아니다 — 아래 양성 대조군이 그 둘을 지킨다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: 이름을 조립한다 — 일괄 삭제 스크립트가 이 가드 자신을 오염시키지 않도록.
_SWEEP = "expire" + "_stale"
_RESULT = "Expire" + "Result"
_NON_TERMINAL = "_NON_TERMINAL_ACTION" + "_STATUSES"
_PROPOSAL_TTL = "_DEFAULT_PROPOSAL" + "_TTL"
_DEFAULT_EXPIRY = "_DEFAULT" + "_EXPIRY"

_SCANNED = ("backend", "plugin", "bsvibe_sdk", "tests")


def _identifiers(tree: ast.AST):
    """실제 식별자만 — docstring·주석·산문은 후보가 아니다(#845 의 교훈)."""
    for node in ast.walk(tree):
        match node:
            case ast.arg(arg=name) | ast.keyword(arg=str() as name):
                yield name
            case ast.Attribute(attr=name) | ast.Name(id=name):
                yield name
            case ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name):
                yield name
            case ast.ClassDef(name=name):
                yield name


def _scan(needles: set[str]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for root_name in _SCANNED:
        root = _ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path == Path(__file__) or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = {n for n in _identifiers(tree) if n in needles}
            if hits:
                found[str(path.relative_to(_ROOT))] = hits
    return found


def test_the_sweep_and_its_result_type_are_gone() -> None:
    offenders = _scan({_SWEEP, _RESULT, _NON_TERMINAL, _PROPOSAL_TTL, _DEFAULT_EXPIRY})
    assert not offenders, "만료 축 식별자가 남았다:\n" + "\n".join(
        f"  {f}: {sorted(s)}" for f, s in sorted(offenders.items())
    )


def test_proposals_and_actions_no_longer_carry_an_expiry() -> None:
    """모델에서 사라져야 한다 — 저장·직렬화가 전부 여기서 파생된다."""
    from backend.knowledge.canonicalization import models

    for name in ("ProposalEntry", "ActionEntry"):
        fields = {f.name for f in __import__("dataclasses").fields(getattr(models, name))}
        assert "expires_at" not in fields, f"{name} 이 아직 만료를 갖는다: {sorted(fields)}"


def test_the_decisions_surfaces_no_longer_publish_a_proposal_expiry() -> None:
    """BREAKING — MCP 와 REST 양쪽에서 사라져야 한다.

    한쪽만 지우면 두 표면이 갈라진다
    ([[mirrored-surface-drifts-in-the-direction-of-least-testing]]).
    """
    from backend.api.v1.decisions._schemas import ProposalResponse

    assert "expires_at" not in ProposalResponse.model_fields

    mcp = (_ROOT / "backend/mcp/tools/decisions_tools.py").read_text(encoding="utf-8")
    assert '"expires_at"' not in mcp, "MCP 제안 응답이 아직 만료를 싣는다"


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_scanner_can_actually_find_a_surviving_symbol() -> None:
    """스캐너가 고장 나 있으면 위 부재 주장은 공짜로 참이 된다."""
    found = _scan({"ProposalEntry"})
    assert len(found) >= 3, f"스캐너가 살아 있는 심볼을 못 찾는다: {sorted(found)}"


def test_the_enforced_expiry_axes_survive() -> None:
    """⭐ 이 PR 의 가장 중요한 대조군 — **철자가 같다고 같은 축이 아니다.**

    ``DecisionEntry`` · ``PolicyEntry`` 의 만료는 실제로 판정에 쓰인다. 지우는
    것은 *강제되지 않는* 축이지 ``expires_at`` 이라는 이름이 아니다.
    """
    import dataclasses

    from backend.knowledge.canonicalization import models

    for name in ("DecisionEntry", "PolicyEntry"):
        fields = {f.name for f in dataclasses.fields(getattr(models, name))}
        assert "expires_at" in fields, f"{name} 의 만료를 잘못 지웠다"

    for module in ("decisions", "policies"):
        src = (_ROOT / f"backend/knowledge/canonicalization/{module}.py").read_text(
            encoding="utf-8"
        )
        assert "expires_at" in src, f"{module}.py 가 만료 판정을 잃었다"


def test_the_store_still_round_trips_decision_and_policy_expiry() -> None:
    """저장 계층에서도 두 축이 살아야 한다 — 모델만 보면 놓친다."""
    src = (_ROOT / "backend/knowledge/canonicalization/store.py").read_text(encoding="utf-8")
    # read_decision / write_decision / read_policy / write_policy — 정확히 네 **줄**.
    # 첫 판은 ``src.count("expires_at")`` 로 셌는데 한 줄에 두 번 나오는 형태가
    # 있어(``expires_at=_parse_iso(fm.get("expires_at"))``) 명제가 어긋났다.
    sites = [ln for ln in src.splitlines() if "expires_at" in ln]
    assert len(sites) == 4, (
        "store 의 만료 취급 자리가 4줄이 아니다 — decision/policy 를 잘못 건드렸다:\n"
        + "\n".join(f"  {s.strip()}" for s in sites)
    )


def test_proposals_and_decisions_still_work() -> None:
    """양성 대조군 — 만료를 떼면서 제안 표면 자체를 깨면 안 된다."""
    from backend.api.v1.decisions._schemas import ProposalResponse

    assert {"id", "status"} <= set(ProposalResponse.model_fields)
