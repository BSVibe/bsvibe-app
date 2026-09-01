# `POST /deliverables/{id}/retract` ↔ `bsvibe_deliverables_retract` — 검증 체크리스트

> 2026-09-01. 핸드오프 §Ⅳ.2 의 남은 갭 셋 중 하나. **여기선 확정 설계가 맞았다** —
> report 와 달리(§아래) 규칙이 정말로 `backend.api` 에 있었다.

## 측정으로 확인한 것 (추측 아님)

계약 엔진에 프로브 모듈을 넣어 실제 도달을 쟀다:

```python
# backend/mcp/tools/_probe_retract.py (일회용)
from backend.api.v1.deliverables._retract_handler import RetractHandler
```

```
backend.mcp is not allowed to import backend.connectors:
    _retract_handler -> backend.connectors.db (l.27)
backend.mcp is not allowed to import backend.extensions:
    _retract_handler -> backend.extensions.plugin.base
backend.mcp is not allowed to import backend.router:
    _retract_handler -> backend.router.accounts.crypto
```

**모듈 레벨 직접 임포트 3개** — 전이가 아니다. 핸드오프가 옳았다.

## 새 형태 — 규칙과 런타임을 가른다

| | |
|---|---|
| `backend/workflow/application/deliverable_retraction.py` | **규칙**: 순서 · 멱등 · all-or-nothing 플립. `RetractOutcome` 반환 |
| `backend/api/v1/deliverables/_retract_handler.py` | **런타임**: `PluginRetractHandler` — 플러그인 레지스트리 · 자격증명 복호 |
| `backend/api/v1/deliverables/retract.py` | 얇은 어댑터 — outcome → HTTP 404/400/502/200 |
| `backend/mcp/tools/workflow_tools.py` | `bsvibe_deliverables_retract` — outcome → `ToolError` |

런타임은 `ToolContext.extras["retract_handler"]` 로 **주입**한다 —
`delivery_dispatcher` · `client_sandbox` 와 같은 선례. 컴포지션 루트는
`backend/api/main.py` lifespan.

⚠️ **핸드오프가 경고한 함정을 피했다.** `build_registry()` 는
`backend/mcp/server.py` 에서 **인자 없이** 불린다. work tools 처럼 "주입 없으면
미등록"으로 했으면 그 경로에서 툴이 사라져 **파리티 가드가 깨진다.** 그래서
**등록은 항상** 하고, 주입 부재는 **호출 시점에 거절**로 드러낸다 — 일어나지
않은 회수를 성공으로 보고하지 않는다.

`lint-imports` **5 kept · 0 broken — 새 예외 0건.**

## 가드

| 테스트 | 명제 |
|---|---|
| `test_deliverables_retract_runs_the_rule_and_marks_the_row` | 저장된 핸들마다 compensate 1회 → `retracted_at` 플립 |
| `test_deliverables_retract_does_not_mark_the_row_when_compensate_fails` | 실패 시 행은 **회수 안 됨** (재시도 가능) |
| `test_deliverables_retract_without_an_injected_handler_refuses` | 주입 부재 = 거절, 크래시도 조용한 no-op 도 아님 |
| `test_build_server_installs_the_retract_handler_into_every_tool_context` | **배선 자체** |

⚠️ 앞의 셋은 `extras` 를 직접 넣는다 — 툴은 증명하지만 **배선은 증명 못 한다**.
그래서 네 번째를 따로 뒀고, **전선을 끊어 빨강을 실증했다**:

```
server.py:  extras["retract_handler"] = retract_handler  →  pass
결과:        assert [None] == [<object ...>]   ✅ 빨강
```

## 체크리스트

- [ ] `uv run pytest --cov=backend --cov=plugin --cov-fail-under=80` → green
- [ ] `uv run lint-imports` → **5 kept, 0 broken**
- [ ] `uv run mypy backend/` → clean
- [ ] `_KNOWN_GAPS` 3 → **2** (양방향 fail-closed — 세지 말고 읽어라)
- [ ] 배포 후 prod: 툴 등록 확인 + **주입이 실제로 도달**했는지(거절 메시지가 아닌지)
