"""감사 B4 — 구성된 적 없는 vault sync 확장점을 지운다.

⚠️ **이 발견은 전수 조사에서 뒤늦게 잡혔다.** 초기 측정이 다중 심볼 grep 오류에
걸려 ``prod=0 tests=0`` 으로 잘못 나왔고, 이후 B1 의 전이 폐포에서 ``graph/sync``
가 *도달* 로 표시되면서 별도로 다뤄지지 않았다. 50건 대조를 하지 않았으면 놓쳤다.

## import 도달 ≠ 실행 도달 (#793 과 같은 모양)

* ``SyncManager`` 는 ``writer_core/_io.py`` · ``_core.py`` 양쪽에서 **``TYPE_CHECKING``
  import** 다 — 타입 주석으로만 쓰인다.
* ``GardenWriter(sync_manager: SyncManager | None = None)`` 이고,
  **``sync_manager=`` 를 넘기는 프로덕션 호출자가 0곳**이다
  (``factory.py`` 가 *"sync_manager … default to None"* 이라 적어뒀다).
* ∴ ``_notify_sync`` 는 **항상 첫 줄에서 반환**한다. 죽은 분기다.

## 나머지도 같다

* ``SyncBackend`` — Protocol 이고 **구현 0개**. docstring 이 *"Implementations
  (e.g. S3SyncBackend, GitSyncBackend) are registered …"* 라 적었지만 그 둘은
  만들어진 적이 없다.
* ``PluginSyncAdapter`` — **소비자 0**. 게다가 ``PluginMeta = Any`` 플레이스홀더에
  의존한다 (``TODO(bundle-k-integration)`` — 끝나지 않은 통합의 잔재).

## ⚠️ 함께 지우면 안 되는 것

``WriteEvent`` / ``WriteEventType`` 은 **살아 있는 GardenWriter 가 쓴다** —
``_core.py`` 가 ``WriteEventType(event_type_str)`` 로 생성한다. 모듈은 남기고
구성된 적 없는 부분만 지운다.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_the_unconfigured_sync_surface_is_gone() -> None:
    sync = importlib.import_module("backend.knowledge.graph.sync")
    for name in ("SyncManager", "SyncBackend", "PluginSyncAdapter"):
        assert not hasattr(sync, name), f"{name} 이 아직 있다"


def test_the_writer_no_longer_takes_a_sync_manager() -> None:
    """넘기는 호출자가 0곳이던 파라미터 — 항상 ``None`` 이었다."""
    from backend.knowledge.graph.writer import GardenWriter

    assert "sync_manager" not in inspect.signature(GardenWriter.__init__).parameters


def test_the_dead_notify_branch_is_gone() -> None:
    src = (_ROOT / "backend/knowledge/graph/writer_core/_core.py").read_text(encoding="utf-8")
    assert "_notify_sync" not in src
    assert "_sync_manager" not in src


def test_no_source_still_names_the_removed_surface() -> None:
    needles = ("Sync" + "Manager", "Sync" + "Backend", "PluginSync" + "Adapter")
    offenders = [
        f"{p.relative_to(_ROOT)}:{i}"
        for tree in (_ROOT / "backend", _ROOT / "tests")
        for p in tree.rglob("*.py")
        if p != Path(__file__)
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        # **코드 형태**만 센다 — 왜 지웠는지 설명하는 산문(백틱 안의 이름)은 남아야
        # 한다. 그 설명이 없으면 다음 사람이 같은 확장점을 다시 만든다.
        if any(n in line for n in needles) and "``" not in line
    ]
    assert not offenders, f"지운 표면을 아직 가리킨다: {offenders[:20]}"


# ── 양성 대조군 ────────────────────────────────────────────────────────────


def test_the_write_event_vocabulary_survives() -> None:
    """살아 있는 ``GardenWriter`` 가 ``WriteEventType(event_type_str)`` 로 만든다."""
    sync = importlib.import_module("backend.knowledge.graph.sync")
    assert hasattr(sync, "WriteEvent")
    assert hasattr(sync, "WriteEventType")


def test_the_garden_writer_still_writes() -> None:
    """양성 대조군 — 볼트 쓰기는 형님이 실제로 쓰는 경로다."""
    from backend.knowledge.graph.writer import GardenWriter

    for name in ("write_seed", "write_note"):
        if hasattr(GardenWriter, name):
            return
    raise AssertionError("GardenWriter 의 쓰기 표면이 사라졌다")


def test_the_vault_modified_event_still_emits() -> None:
    """양성 대조군 — sync 와 무관한 이벤트 발화는 그대로여야 한다."""
    src = (_ROOT / "backend/knowledge/graph/writer_core/_core.py").read_text(encoding="utf-8")
    assert "_emit_vault_modified" in src
