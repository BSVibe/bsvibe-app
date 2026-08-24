"""볼트 쓰기 이벤트의 어휘.

한때 ``SyncManager`` / ``SyncBackend`` / ``PluginSyncAdapter`` 로 "쓰기 후 외부로
동기화" 확장점이 있었지만 **구성된 적이 없다** — ``GardenWriter(sync_manager=…)`` 에
값을 넘기는 프로덕션 호출자가 0곳이라 알림 분기는 항상 첫 줄에서 반환했고,
``SyncBackend`` 구현체(docstring 이 약속한 S3/Git)는 만들어진 적이 없다.
2026-08-24 에 지웠다.

남은 :class:`WriteEvent` / :class:`WriteEventType` 은 살아 있는 ``GardenWriter`` 가
쓴다 — 문자열 이벤트 종류를 이 어휘로 검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.core.plugin_loader import PluginMeta
    # TODO(bundle-k-integration): out-of-scope source dep -- original: from bsage.core.runtime_config import RuntimeConfig
    PluginMeta = Any
    RuntimeConfig = Any

import structlog

logger = structlog.get_logger(__name__)


class WriteEventType(Enum):
    """Type of vault write operation."""

    SEED = "seed"
    GARDEN = "garden"
    ACTION = "action"


@dataclass
class WriteEvent:
    """Describes a vault write that just occurred."""

    event_type: WriteEventType
    path: Path
    source: str
