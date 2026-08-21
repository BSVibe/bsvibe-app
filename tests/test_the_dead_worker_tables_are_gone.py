"""감사 D3 — ``backend/workers/db.py`` 의 네 테이블 중 셋은 read/write 가 0이다.

**이름 충돌이 이 발견의 핵심이다.** ``WorkerRow`` 는 **두 곳**에 정의돼 있다:

============================================  ==================  ==========  ==========
클래스                                          테이블                prod 행수    판정
============================================  ==================  ==========  ==========
``backend.executors.db.WorkerRow``            ``executor_workers``          8   **현역**
``backend.workers.db.WorkerRow``              ``workers``                   0   삭제
============================================  ==================  ==========  ==========

심볼 이름으로 소비자를 세면 ``WorkerRow`` 는 14개 파일에서 쓰이는 것처럼 보인다.
**import 경로로 세면 전부 ``backend.executors.db`` 쪽이다** — ``workers.db`` 것을
쓰는 곳은 ``workers/__init__.py`` 의 재수출 하나뿐이다. (같은 함정을 이 감사에서
``knowledge.graph`` vs ``knowledge.code_graph`` 로 이미 한 번 겪었다.)

``executors/db.py`` 는 이름 충돌을 알고 있었다 — *"The name ``workers`` is already
taken there, so this subsystem owns its own table: ``executor_workers``."*
그리고 Lift E5 에서 **자기 쪽** install-token 테이블을 지웠다. 같은 아이디어의
``workers/db.py`` 쪽 테이블은 그때 함께 정리되지 않고 남아 있었다.

``SettleDrainRow`` (``settle_drains``, prod **130행**) 은 현역이라 남긴다 —
settle worker · trust surface · deliverable narrative 가 읽는다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

_DEAD_NAMES = (
    ("backend.workers.db", "WorkerRow"),
    ("backend.workers.db", "WorkerInstallTokenRow"),
    ("backend.workers.db", "AuditRelayStateRow"),
    ("backend.workers", "WorkerRow"),
    ("backend.workers", "WorkerInstallTokenRow"),
    ("backend.workers", "AuditRelayStateRow"),
)

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (_ROOT / "backend", _ROOT / "tests")


@pytest.mark.parametrize(("module", "name"), _DEAD_NAMES)
def test_the_unwritten_table_orm_is_gone(module: str, name: str) -> None:
    assert not hasattr(importlib.import_module(module), name), f"{module}.{name} 가 아직 있다"


def test_the_dead_tables_are_not_in_the_metadata() -> None:
    """ORM 심볼이 아니라 **메타데이터**를 본다 — 마이그레이션이 보는 것이 이쪽이다."""
    from backend.workers.db import WorkersBase

    assert {"workers", "worker_install_tokens", "audit_relay_state"}.isdisjoint(
        WorkersBase.metadata.tables
    )


def test_no_source_still_points_at_the_deleted_orm() -> None:
    needles = ("WorkerInstallTokenRow", "AuditRelayStateRow")
    offenders = [
        f"{path.relative_to(_ROOT)}:{i}"
        for tree in _TREES
        for path in tree.rglob("*.py")
        if path != Path(__file__) and "migrations" not in path.parts
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if any(needle in line for needle in needles)
    ]
    assert not offenders, f"삭제된 ORM 을 아직 가리킨다: {offenders}"


def test_the_live_drain_table_survives() -> None:
    """양성 대조군 — ``settle_drains`` 는 prod 130행. settle worker 가 쓴다."""
    from backend.workers.db import SettleDrainRow, WorkersBase

    assert SettleDrainRow.__tablename__ == "settle_drains"
    assert "settle_drains" in WorkersBase.metadata.tables


def test_the_live_executor_worker_registry_survives() -> None:
    """양성 대조군 — **이름이 같은 살아 있는 쪽**. prod 8행, 워커 레지스트리 SoT."""
    from backend.executors.db import WorkerRow as ExecutorWorkerRow

    assert ExecutorWorkerRow.__tablename__ == "executor_workers"


def test_the_worker_rest_and_mcp_surfaces_still_resolve() -> None:
    """양성 대조군 — 형님이 워커를 등록/조회하는 표면은 그대로여야 한다."""
    api = importlib.import_module("backend.api.v1.workers")
    mcp = importlib.import_module("backend.mcp.tools.workers_tools")
    assert hasattr(api, "router")
    assert hasattr(mcp, "register_workers_tools")
