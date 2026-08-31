"""원본 레이어 — 런타임 원본을 vault 에 **불변으로** 남긴다.

형님 지시(2026-08-31): *"실제 사용 과정에서 사용자의 요청, 피드백, 혹은 스스로
깨달은 회고 등이 원본 그대로 저장 되어야 한다. 상위 지식은 여러 원본이 합쳐진
것이기 때문에 변경 등이 될 수 있지만, 원본 지식은 히스토리성이기 때문에 보존된다."*

측정된 격차(prod 2026-08-31) — 요청 223건 · 피드백 41건 · settle 147건이 DB
운영 테이블에만 있고 vault 에는 0건이었다. 셋 다 ``execution_runs`` 에
``ON DELETE CASCADE`` 로 묶여 있어 런을 지우면 원본도 함께 사라진다. vault 로
가던 것은 :mod:`~backend.knowledge.infrastructure.workers.settle_worker` 의
싱크가 만드는 **가공된 관찰 노트**뿐이고, 그마저 게이트를 통과한 것만이다.

## write_seed 와의 차이 — 왜 별도 함수인가

:meth:`GardenWriter.write_seed` 는 파일명을 **캡처 시각**으로 짓는다. 그것은
"긁어온 것을 계속 덧붙이는" 용도에 맞고, 원본 레이어에는 맞지 않는다. 원본은
DB 행 하나당 정확히 하나여야 하고, 백필과 실시간 기록이 같은 행을 두 번
건드려도 두 번째가 과거를 덮으면 안 된다. 그래서 파일명이 **키**에서
결정론적으로 나온다.

## 왜 ``Vault`` 를 통째로 받는가

워크스페이스 vault 위치의 정의는
:func:`~backend.knowledge.graph.vault_paths.workspace_vault_root` 하나뿐이어야
한다 (``tests/knowledge/test_one_vault_root_definition.py``). ``vault_root`` 와
``workspace_id`` 를 따로 받으면 이 모듈이 레이아웃을 두 번째로 조립하게 되고,
region 축이 남긴 교훈이 그대로 재발한다. 호출자는
``KnowledgeFactory(...).vault()`` 로 이미 스코프된 것을 넘긴다.

## 왜 ``seeds/`` 아래인가

``seeds`` 는 이미 note 열람 화이트리스트(:data:`backend.api.v1.inside.note._NOTE_DIRS`)
와 검색 색인 카테고리(:meth:`FileIndexReader._resolve_categories`) **양쪽에**
있다. 원본을 여기 떨구면 GUI 열람과 검색이 인프라 추가 없이 따라온다 — 없던
것은 서브시스템이 아니라 연결 하나였다.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog

from backend.knowledge._internal.exceptions import VaultPathError
from backend.knowledge.graph.note import build_frontmatter

if TYPE_CHECKING:
    from pathlib import Path

    from backend.knowledge.graph.vault import Vault

logger = structlog.get_logger(__name__)

#: 원본의 세 종류. 각각 ``seeds/<kind>/`` 로 간다.
#:
#: * ``request``   — 형님이 런을 시작시킨 지시문 (가공 전 원문)
#: * ``feedback``  — 형님이 체크포인트에 쓴 답변 (원클릭 버튼은 0자라 제외된다)
#: * ``retrospect``— 작업 에이전트가 스스로 선언한 회고(``agent_knowledge``)
OriginalKind = Literal["request", "feedback", "retrospect"]

ORIGINAL_KINDS: tuple[OriginalKind, ...] = ("request", "feedback", "retrospect")

#: 파일명에 쓸 수 없는 문자를 걷어낸다. 키는 보통 UUID 라 무해하지만, 키의
#: 출처가 늘어나도 경로 조립이 안전하도록 여기서 한 번 좁힌다. ``Vault``
#: 의 traversal 차단이 뒤에 한 겹 더 있다.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

#: 파일명 상한. macOS/ext4 의 255바이트 한계 아래로 넉넉히 자른다.
_MAX_KEY_CHARS = 120


def _safe_key(key: str) -> str:
    """키를 파일명 한 조각으로 좁힌다 (빈 문자열이면 ``""``)."""
    return _UNSAFE_IN_FILENAME.sub("-", key).strip("-")[:_MAX_KEY_CHARS]


async def record_original(
    *,
    vault: Vault,
    kind: OriginalKind,
    key: str,
    title: str,
    content: str,
    provenance: dict[str, Any] | None = None,
) -> Path | None:
    """원본 하나를 ``seeds/<kind>/<key>.md`` 에 기록한다.

    Returns:
        새로 쓴 파일 경로. **이미 있으면 ``None``** (덮어쓰지 않는다) — 빈
        본문이거나 기록에 실패했을 때도 ``None``.

    호출자에게 예외를 던지지 않는다. 원본 기록은 런의 부수 효과이지 본업이
    아니므로, vault 가 못 쓰는 상태라고 런이 죽으면 안 된다. 실패는 로그로
    남아 prod 에서 셀 수 있다.
    """
    if not content.strip():
        # 0자 원본은 원본이 아니다. settle 싱크가 이미 같은 판단을 한다 —
        # 원클릭 승인은 형님이 쓴 글자가 없으므로 노트를 얻지 않는다.
        logger.info("original_skipped_empty", kind=kind, key=key)
        return None

    safe = _safe_key(key)
    if not safe:
        logger.warning("original_skipped_unusable_key", kind=kind, key=key)
        return None

    try:
        path = vault.resolve_path(f"seeds/{kind}/{safe}.md")
    except VaultPathError:
        logger.warning("original_path_rejected", kind=kind, key=key)
        return None

    metadata: dict[str, Any] = {
        "type": "original",
        "kind": kind,
        "key": key,
        "title": title,
        "recorded_at": datetime.now(tz=UTC).isoformat(),
    }
    if provenance:
        # 파생 지식에서 원본으로 되짚어 올 수 있게 출처를 프론트매터에 박는다.
        # 시스템 필드가 이기도록 provenance 를 먼저 깔고 위에 덮는다.
        metadata = {**provenance, **metadata}

    document = f"{build_frontmatter(metadata)}\n{content}\n"

    try:
        return await asyncio.to_thread(_write_once, path, document)
    except OSError:
        # 디렉터리 자리를 파일이 차지하고 있거나, 디스크가 꽉 찼거나, 권한이
        # 없는 경우. 전부 런과 무관한 사고이므로 삼키고 센다.
        logger.warning("original_write_failed", kind=kind, key=key, exc_info=True)
        return None


def _write_once(path: Path, document: str) -> Path | None:
    """이미 없을 때만 쓴다 — 원자적으로.

    ``exists()`` 로 먼저 보고 쓰면 두 워커가 같은 키를 동시에 기록할 때 뒤엣
    것이 앞엣 것을 덮는다. ``O_EXCL`` 은 그 창을 커널이 닫아 준다: 파일이 이미
    있으면 ``FileExistsError``, 곧 "과거가 이겼다".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(document)
    except FileExistsError:
        logger.debug("original_already_recorded", path=str(path))
        return None
    logger.info("original_recorded", path=str(path))
    return path


__all__ = ["ORIGINAL_KINDS", "OriginalKind", "record_original"]
