"""교차테넌트 읽기는 이름으로 청해야 하고, 청하는 곳은 여기 적혀 있어야 한다 (#959).

``'*'`` GUC 는 RLS 정책의 ``USING`` 을 모든 테넌트에 연다. 그걸 세우는 두 헬퍼
(``cross_tenant_read`` · ``cross_tenant_session_read``)를 부르는 파일이 핀과
다르면 빨개진다 — 새 사용처는 **이 목록에 한 줄을 더하는 결정**을 거쳐야 한다.

호출은 AST 로 센다. 문자열로 세면 제거 내력을 적은 주석·독스트링에 먼저 걸린다.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HELPERS = frozenset({"cross_tenant_read", "cross_tenant_session_read"})

#: 파일 → 왜 테넌트를 넘어 읽어야 하나.
ALLOWED: dict[str, str] = {
    "backend/workflow/infrastructure/workers/daily_brief_worker.py": "브리프 대상 테넌트 열거",
    "backend/workflow/infrastructure/workers/auth_dependency_worker.py": "장애 공지 대상 테넌트 열거",
    "backend/knowledge/infrastructure/workers/settle_worker.py": "배치에 섞인 테넌트들의 정책 조회",
    "plugin/audit/retention_sweep.py": "보존 기간을 둔 테넌트 열거",
    "backend/workflow/infrastructure/workers/intake_worker.py": (
        "드레인 안 된 트리거 스캔 — NOT EXISTS(requests) 가 모든 테넌트를 봐야 한다"
    ),
    "backend/identity/infrastructure/repositories/workspace_repository_sql.py": (
        "사용자의 워크스페이스 목록 — 자기 멤버십 조인이 범위를 좁힌다"
    ),
    "backend/workflow/application/runtime/bootstrap_anchor_backfill.py": (
        "운영 CLI — 완료된 제품을 테넌트 가로질러 고른다"
    ),
    "backend/workflow/application/decision_answer_drain.py": "채팅 답이 큐에 든 Decision 스캔",
    "backend/workflow/infrastructure/workers/agent_worker.py": (
        "디스크 리퍼가 살아 있는 런·제품을 묻는다 · 큐 클레임(요청·런) · 멈춘 클레임 회수"
    ),
}


def _callers() -> set[str]:
    found: set[str] = set()
    for base in ("backend", "plugin"):
        for path in (_ROOT / base).rglob("*.py"):
            rel = path.relative_to(_ROOT).as_posix()
            if rel == "backend/data/rls.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name in _HELPERS:
                    found.add(rel)
    return found


def test_cross_tenant_read_callers_are_exactly_the_allowlist() -> None:
    assert _callers() == set(ALLOWED)
