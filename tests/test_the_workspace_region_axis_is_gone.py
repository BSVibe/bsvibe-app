"""워크스페이스별 ``region`` 축을 없앤다 — 강제된 적 없는 축이다.

#844 가 **읽는 쪽**을 정의 하나로 모았다. 이 PR 은 **축 자체**를 지운다.

## 실측이 보여준 것 (#844, 2026-08-28)

* **이 값으로 분기하는 코드 0** — region 이 하는 일은 vault 경로 세그먼트 하나뿐이었다.
  라우팅도, 샤딩도, 데이터 레지던시 강제도 없다
* **두 답이 이미 갈라져 있었다** — 쓰는 쪽은 ``WorkspaceRow.region``, REST 는
  ``settings.knowledge_default_region``. prod 가 단일 리전이라 안 보였을 뿐
* **API 는 임의 값을 받았다** — create 와 PATCH 양쪽. 기본값에서 한 필드만 어긋나면
  settle 이 쓰는 디렉터리와 REST 가 읽는 디렉터리가 갈라졌다
* **규정준수 표면이 거짓을 주장했다** — Art.30 기록이 워크스페이스별 레지던시를
  공시했는데 Supabase 프로젝트는 하나다

이 저장소는 같은 모양의 축을 이미 폐기했다 — ``20260824_drop_data_jurisdiction.py``,
*"강제된 적 없는 축을 지운다"*. region 이 그 형제다.

## 남는 것

``settings.knowledge_default_region`` 은 **배포 상수로 남는다** — vault 레이아웃
``<vault_root>/<region>/<workspace_id>/`` 의 가운데 세그먼트를 계속 만든다.
지우는 것은 **워크스페이스마다 다를 수 있다는 주장**이지 디렉터리 이름이 아니다.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

#: 이름을 조립한다 — 일괄 rename/삭제 스크립트가 이 가드 자신을 오염시키지 않도록.
#: (#809 에서 needle 이 아니라 **조건 줄**이 통째로 지워져 가드가 초록이 된 적이 있다.)
_AXIS = "reg" + "ion"


def test_the_column_is_gone_from_the_orm() -> None:
    """ORM 매핑에서 사라져야 한다 — 선례 가드는 이 자리를 ``pass`` 로 비워뒀다."""
    from backend.identity.workspaces_db import WorkspaceRow

    assert _AXIS not in WorkspaceRow.__table__.columns, sorted(
        c.name for c in WorkspaceRow.__table__.columns
    )
    assert not hasattr(WorkspaceRow, _AXIS)


def test_the_api_no_longer_accepts_or_returns_it() -> None:
    """create 와 PATCH 가 이 값을 받던 것이 드리프트를 **도달 가능**하게 만들었다."""
    from backend.api.v1 import workspaces

    for name in ("WorkspaceCreate", "WorkspaceUpdate", "WorkspaceResponse"):
        model = getattr(workspaces, name)
        assert _AXIS not in model.model_fields, f"{name} 이 아직 {_AXIS} 을 갖는다"


def test_the_mcp_workspace_surface_no_longer_carries_it() -> None:
    from backend.mcp.tools.workspace_tools import WorkspaceGetOutput

    assert _AXIS not in WorkspaceGetOutput.model_fields


def test_signup_no_longer_stamps_a_per_workspace_region() -> None:
    """``ensure_user_bootstrapped(region=...)`` 가 축의 **생산자**였다."""
    import inspect

    from backend.config import Settings
    from backend.identity.service import ensure_user_bootstrapped

    assert _AXIS not in inspect.signature(ensure_user_bootstrapped).parameters
    assert ("default_workspace_" + _AXIS) not in Settings.model_fields


def test_no_source_still_names_a_per_workspace_region() -> None:
    """파일 집합을 핀으로 박는다 — 패턴 목록이 아니라. 그리고 **AST 로** 센다.

    이 가드의 앞 세대는 두 번 틀렸다.

    1. 자기가 본 적 있는 철자(``row.region``, ``WorkspaceRow.region,``)를
       나열해서 ``ws.region`` 과 ``target.region`` 두 판독기를 통과시켰다.
       **목록형 가드는 저자가 몇 가지 형태를 떠올렸는지를 증명할 뿐이다.**
    2. 그래서 grep 을 넓게 잡았더니 이번엔 **산문 213줄**을 물었다 — 왜 이 축을
       지웠는지 설명하는 docstring 이 자기 이름을 부르기 때문이다. 백틱으로
       거르는 건 미봉책이다: 백틱 없는 docstring 한 줄이면 다시 샌다.

    그래서 텍스트가 아니라 **코드**를 센다. AST 를 걸으면 docstring·주석·문자열
    리터럴은 애초에 후보가 아니고, 남는 것은 실제 식별자뿐이다. 이름은
    ``ast.arg`` / ``ast.keyword`` / ``ast.Attribute`` / ``ast.Name`` /
    대입 타깃에서만 나온다.
    """
    import ast

    trees = (_ROOT / "backend", _ROOT / "plugin", _ROOT / "bsvibe_sdk", _ROOT / "tests")

    #: 남아도 되는 이름 — 배포 상수 그 자체.
    _CONSTANT = "knowledge_default_" + _AXIS

    def _names(tree: ast.AST):
        for node in ast.walk(tree):
            match node:
                case ast.arg(arg=name):
                    yield name
                case ast.keyword(arg=str() as name):
                    yield name
                case ast.Attribute(attr=name):
                    yield name
                case ast.Name(id=name):
                    yield name

    offenders: dict[str, set[str]] = {}
    for root in trees:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if path == Path(__file__) or "migrations" in path.parts:
                continue
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            hits = {n for n in _names(module) if _AXIS in n and n != _CONSTANT}
            if hits:
                offenders[str(path.relative_to(_ROOT))] = hits

    allowed = {
        # 무관한 축: S3 SigV4 서명이 요구하는 리전 문자열(R2 는 리전이 없다).
        "backend/storage/product_bundle_store.py": {"k_region", "_SIGV4_REGION"},
        # Art.30 기록 — 처리가 **실제로** 일어나는 리전(배포 상수)을 보고한다.
        "backend/api/v1/workspace_compliance.py": {"region"},
        # 커넥터 임포트 메타데이터의 동명이축. 파운더가 PWA 커넥터 설정에서
        # 실제로 채울 수 있는 필드(``default_region``)이고 기본값은 ``"imported"`` —
        # vault 리전이 아니라 임포트 통계에 붙는 라벨이다. 아무것도 라우팅하지
        # 않으므로 이 축과 같은 운명이 맞지만, 사용자 표면을 지우는 것은
        # 리팩터가 아니라 제품 결정이라 별도로 다룬다.
        "plugin/claude/plugin.py": {"region", "resolved_region"},
        "plugin/gpt/plugin.py": {"region", "resolved_region"},
        "plugin/notion/plugin.py": {"region", "resolved_region"},
        "plugin/obsidian/plugin.py": {"region", "resolved_region"},
    }

    survivors = {
        f: sorted(names)
        for f, names in offenders.items()
        if f not in allowed or (allowed[f] is not None and not names <= allowed[f])
    }
    assert not survivors, f"워크스페이스별 {_AXIS} 식별자가 남았다:\n" + "\n".join(
        f"  {f}: {ns}" for f, ns in sorted(survivors.items())
    )

    # 승인 목록이 썩는 것도 막는다 — 더 이상 쓰지 않는 파일이 남아 있으면
    # 그 파일에 생긴 **진짜 새 사용처**를 가려준다.
    stale = sorted(set(allowed) - set(offenders))
    assert not stale, f"승인 목록이 더 이상 {_AXIS} 을 쓰지 않는 파일을 가리킨다: {stale}"


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_vault_layout_still_has_its_region_segment() -> None:
    """배포 상수는 살아남는다 — 지우는 건 축이지 디렉터리 이름이 아니다."""
    import uuid

    from backend.config import get_settings
    from backend.knowledge.graph.vault_paths import workspace_vault_root

    workspace_id = uuid.uuid4()
    root = workspace_vault_root(workspace_id)

    assert root.parts[-1] == str(workspace_id)
    assert root.parts[-2] == get_settings().knowledge_default_region


def test_the_workspace_surface_still_works() -> None:
    """양성 대조군 — 워크스페이스 자체는 형님이 실제로 쓰는 표면이다."""
    from backend.api.v1.workspaces import WorkspaceCreate, WorkspaceResponse

    assert "name" in WorkspaceCreate.model_fields
    assert {"id", "name", "safe_mode"} <= set(WorkspaceResponse.model_fields)


def test_signup_still_creates_a_workspace_and_membership() -> None:
    """양성 대조군 — 축을 떼면서 부트스트랩 자체를 깨면 안 된다."""
    import inspect

    from backend.identity.service import ensure_user_bootstrapped

    params = inspect.signature(ensure_user_bootstrapped).parameters
    assert {"supabase_user_id", "email"} <= set(params)
