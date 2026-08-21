"""감사 C2 — bootstrap 상태 어휘가 세 벌이고, **선언된 SoT 는 import 0** 이었다.

``product_bootstrap_runtime.py`` 가 상수를 선언하며 이유까지 적어뒀다:

    *"Lifecycle vocabulary — kept here so the API surface + the PWA can agree on
    the exact strings **without a free-string drift**."*

그런데 그 상수를 import 하는 곳이 **0곳**이다. 대신 in-flight 판정이
``frozenset({"pending", "cloning", "analyzing", "ingesting"})`` 로 **바이트 동일하게
두 벌** 있었다 — ``api/v1/products/bootstrap_actions.py`` 와
``mcp/tools/workflow_tools.py``. 취소/재시도가 "이 제품이 아직 도는 중인가"를
각자의 사본으로 판정한다.

**SoT 가 없어서 생긴 문제가 아니라 있는데 안 쓴 문제다** — B11(#798)과 같은 모양이다.

## 왜 ``backend.common`` 인가

MCP 는 계약상 ``backend.workflow`` 를 import 할 수 있지만,
``product_bootstrap_runtime`` 은 의존 사슬이 무겁다. :mod:`backend.common.settle_kinds`
docstring 이 정확히 그 함정을 경고한다 — 무거운 모듈에서 상수를 가져오면 그 사슬이
통째로 딸려와 계약을 깬다. 어휘만 leaf 로 내리고 런타임이 그것을 재수출한다.

## ⚠️ 이 어휘는 DB 에 이미 쌓인 값이다

``products.bootstrap_status`` 행이 이 문자열을 그대로 담고 있다. 값을 **바꾸는**
리팩터가 아니라 **한 곳에서 읽게 하는** 리팩터다 — 가드가 값 자체를 고정한다.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"


def test_the_in_flight_set_is_built_once() -> None:
    """in-flight 판정이 트리 전체에 **한 곳**에서만 만들어져야 한다."""
    sites = [
        f"{p.relative_to(_ROOT)}:{i}"
        for p in _BACKEND.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if "IN_FLIGHT_STATUSES = frozenset" in line
    ]
    assert len(sites) == 1, f"in-flight 집합이 여러 곳에서 만들어진다: {sites}"
    assert sites[0].startswith("backend/common/"), f"leaf 가 아닌 곳에 있다: {sites[0]}"


def test_both_surfaces_share_the_same_frozenset_object() -> None:
    """특성화 — REST 와 MCP 가 **같은 객체**를 쓴다 (미러가 아니라 공유)."""
    from backend.api.v1.products import bootstrap_actions as rest
    from backend.common.bootstrap_status import IN_FLIGHT_STATUSES
    from backend.mcp.tools import workflow_tools as mcp

    assert rest._IN_FLIGHT_STATUSES is IN_FLIGHT_STATUSES  # noqa: SLF001
    assert mcp._IN_FLIGHT_STATUSES is IN_FLIGHT_STATUSES  # noqa: SLF001


def test_the_runtime_now_uses_the_declared_sot() -> None:
    """감사의 핵심 — SoT 를 선언해놓고 **import 0** 이던 상태를 끝낸다."""
    from backend.common import bootstrap_status as sot
    from backend.workflow.application.runtime import product_bootstrap_runtime as rt

    for name in ("STATUS_PENDING", "STATUS_CLONING", "STATUS_ANALYZING", "STATUS_INGESTING"):
        assert getattr(rt, name) is getattr(sot, name), name


def test_the_stored_vocabulary_is_unchanged() -> None:
    """양성 대조군 — ``products.bootstrap_status`` 에 **이미 쌓인 값**이다.

    이 리팩터는 값을 바꾸지 않는다. 바꾸면 기존 행의 의미가 달라진다."""
    from backend.common.bootstrap_status import (
        IN_FLIGHT_STATUSES,
        STATUS_COMPLETE,
        STATUS_FAILED_CLONE,
        STATUS_FAILED_INGEST,
        STATUS_FAILED_TOO_LARGE,
        STATUS_SKIPPED_CLIENT_ATTACH,
    )

    assert IN_FLIGHT_STATUSES == frozenset({"pending", "cloning", "analyzing", "ingesting"})
    assert STATUS_COMPLETE == "complete"
    assert STATUS_SKIPPED_CLIENT_ATTACH == "skipped:client_attach"
    assert STATUS_FAILED_CLONE == "failed:clone"
    assert STATUS_FAILED_TOO_LARGE == "failed:too_large"
    assert STATUS_FAILED_INGEST == "failed:ingest"


def test_the_common_leaf_still_imports_no_bounded_context() -> None:
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
