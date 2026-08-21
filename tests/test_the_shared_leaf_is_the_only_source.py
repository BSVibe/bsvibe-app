"""감사 C6 · B11 — 두 표면이 손으로 미러하던 것을 ``backend.common`` leaf 로 내린다.

## 왜 ``backend.common`` 인가 — 코드베이스가 이미 그 답을 적어뒀다

``backend/common/settle_kinds.py`` 의 docstring:

    *"``backend.common`` 은 아무것도 import 하지 않는 leaf 라 누구나 안전하게
    의존한다."* — 상수를 ``checkpoint_resolution`` 에서 가져오면 그 의존 사슬이
    통째로 딸려와 **MCP 컨텍스트의 import 계약을 깬다**.

C6 은 그 문제를 **정반대로** 풀어놨다. ``mcp/tools/skills_tools.py`` 가 스스로 적었다:

    *"Kept local so the MCP module doesn't reach into the forbidden
    ``backend.api`` subtree. … Mirrors ``backend.api.v1.skills._slugify`` 1:1."*

**계약이 보안 방어의 사본을 강제한 것이다.** 두 ``_slugify`` 는 경로 탈출
(``..`` · ``/`` · ``\\``)을 막는 같은 검사이고, 한쪽만 고쳐지면 다른 표면은
뚫린 채로 남는다. 실측으로 두 구현의 동작이 아직 같음을 확인하고(드리프트 전),
leaf 로 내려 하나로 만든다.

## B11 — 선언된 SoT 를 세 곳이 무시하고 리터럴을 다시 박았다

``settle_kinds.py`` 는 이미 SoT 로 존재하고 **4곳이 제대로 쓴다.** 그런데
``worth_remembering.py`` · ``decision_note_locator.py`` · ``settle_worker.py`` 는
``"decision_resolution"`` / ``"negative_pattern"`` 을 리터럴로 다시 박았다.
SoT 가 없어서 생긴 문제가 아니라 **있는데 안 쓴** 문제다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"


# ── C6 — 구조 가드 ────────────────────────────────────────────────────────


def test_the_path_traversal_check_lives_in_exactly_one_place() -> None:
    """**보안 불변식** — ``".." in name`` 검사가 트리 전체에 한 곳이어야 한다.

    ⚠️ "slugify 라는 이름의 함수가 하나"가 아니다. 실측해보니 ``_slugify`` 계열이
    **네 곳**에 있고 셋은 **의미가 다르다** — ``knowledge/graph/note.slugify`` 는
    한글/일본어/중국어를 **의도적으로 보존**하고(스킬 쪽은 ASCII 로 깎는다),
    ``code_graph/parser._slugify_heading`` 은 ``"untitled"`` 로,
    ``mcp/tools/knowledge_tools._slugify`` 는 ``"note"`` 로 폴백한다.
    **이름이 같다고 합치면 한국어 노트 파일명이 깨진다.**

    그래서 이 가드는 이름이 아니라 **방어 자체**를 센다."""
    sites = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _BACKEND.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if '".." in name' in line
    ]
    assert len(sites) == 1, f"경로 탈출 방어가 여러 곳에 있다: {sites}"
    assert sites[0].startswith("backend/common/"), f"leaf 가 아닌 곳에 있다: {sites[0]}"


def test_both_surfaces_use_the_same_slugify_object() -> None:
    """특성화 — REST 와 MCP 가 **같은 함수 객체**를 쓴다 (미러가 아니라 공유)."""
    from backend.api.v1 import skills as rest
    from backend.common.slug import slugify
    from backend.mcp.tools import skills_tools as mcp

    assert rest._slugify is slugify  # noqa: SLF001
    assert mcp._slugify is slugify  # noqa: SLF001


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Hello World", "hello-world"),
        ("  Mixed CASE  ", "mixed-case"),
        ("café au lait", "caf-au-lait"),
        # 경로 탈출 — 이 셋이 이 함수의 존재 이유다
        ("../etc/passwd", None),
        ("a/b", None),
        ("a\\b", None),
        # 슬러그를 만들 수 없는 이름
        ("", None),
        ("---", None),
        ("9lives", None),
    ],
)
def test_slugify_behaviour_is_unchanged(name: str, expected: str | None) -> None:
    """특성화 — 통합 전 두 구현의 실측 동작을 그대로 고정한다."""
    from backend.common.slug import slugify

    assert slugify(name) == expected


# ── B11 — 구조 가드 ───────────────────────────────────────────────────────


#: ``"decision_resolution"`` 은 **두 축에서** 쓰이는 같은 문자열이다.
#:
#: * settle payload 의 ``kind`` — ``backend.common.settle_kinds`` 가 SoT (이 가드의 대상)
#: * ``TriggerKind`` — 런이 **어떻게 촉발됐나** (webhook / schedule / direct /
#:   decision_resolution). 별개 어휘이고 Postgres ENUM 으로 고정돼 있다.
#:
#: 두 축을 합치면 안 된다 — 한쪽 값을 바꾸면 다른 쪽 ENUM 이 깨진다. 아래는
#: **TriggerKind 축**이라 이 가드에서 제외한다.
_TRIGGER_KIND_AXIS = {
    "backend/workflow/infrastructure/intake/db.py",
    "backend/workflow/domain/incoming.py",
}


def test_no_module_rehardcodes_the_settle_kind_literals() -> None:
    """``settle_kinds`` 가 SoT 다 — 그 값을 리터럴로 다시 박은 곳이 없어야 한다.

    문자열 grep 이 아니라 **AST 로 실제 코드 상수만** 센다 — 이 값은 설계를 설명하는
    산문에도 자주 나오고, 문서에서 이름을 부르는 것은 재하드코딩이 아니다.

    ⚠️ 마이그레이션은 제외한다 — 이미 적용된 DDL 의 값은 **역사**라서 상수로 바꾸면
    나중에 상수가 바뀔 때 과거 마이그레이션의 의미가 달라진다."""
    sot = _BACKEND / "common" / "settle_kinds.py"
    wanted = {"decision_resolution", "negative_pattern"}
    offenders: list[str] = []
    for path in _BACKEND.rglob("*.py"):
        if (
            path == sot
            or "migrations" in path.parts
            or str(path.relative_to(_ROOT)) in _TRIGGER_KIND_AXIS
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        offenders += [
            f"{path.relative_to(_ROOT)}:{n.lineno}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and n.value in wanted and id(n) not in docstrings
        ]
    assert not offenders, f"SoT 를 무시하고 리터럴을 박았다: {offenders}"


def test_the_settle_kind_sot_still_carries_both_values() -> None:
    """양성 대조군 — 값 자체가 바뀌면 볼트에 이미 쌓인 노트를 못 읽는다."""
    from backend.common.settle_kinds import (
        DECISION_RESOLUTION_SETTLE_KIND,
        NEGATIVE_PATTERN_SETTLE_KIND,
    )

    assert DECISION_RESOLUTION_SETTLE_KIND == "decision_resolution"
    assert NEGATIVE_PATTERN_SETTLE_KIND == "negative_pattern"


def test_the_common_leaf_imports_no_bounded_context() -> None:
    """양성 대조군 — leaf 규율. 여기가 무거워지면 MCP 계약이 깨진다."""
    contexts = (
        "backend.api",
        "backend.router",
        "backend.knowledge",
        "backend.workflow",
        "backend.identity",
        "backend.schedule",
        "backend.extensions",
        "backend.executors",
        "backend.connectors",
        "backend.mcp",
    )
    offenders = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in (_BACKEND / "common").rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith(("import ", "from ")) and any(c in line for c in contexts)
    ]
    assert not offenders, f"common leaf 가 바운디드 컨텍스트를 import 한다: {offenders}"
