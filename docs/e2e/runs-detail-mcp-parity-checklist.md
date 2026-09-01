# `GET /runs/{id}/detail` ↔ `bsvibe_runs_detail` — 검증 체크리스트

> 2026-09-01. 핸드오프 §Ⅳ.2 의 남은 갭 4개 중 하나. **확정 설계가 몰랐던 사실**이
> 하나 있어서 형님 판단을 받고 진행했다.

## 갭의 정체는 "툴을 안 썼다"가 아니었다

`detail` 빌더가 쓰는 `_helpers.py`(293줄)·`_schemas.py`(209줄)는 **`backend.api`
의존이 0개**였다 — stdlib · pydantic · `backend.workflow.infrastructure.db` 뿐.
**이미 workflow 컨텍스트 코드가 api 패키지에 앉아 있었다.** 그래서 주입도, 새
import 예외도 필요 없는 **순수 이동**이었다.

⚠️ 다만 **Lift M1 가드가 그 두 파일을 이름으로 핀**해 두고 있었다:

```python
# tests/glue/test_liftM1_rest_handler_split.py — EXPECTED_SUBMODULES
"backend.api.v1.runs._schemas",
"backend.api.v1.runs._helpers",
```

옮기면 가드가 빨강이 된다. **가드를 내 변경에 맞춰 고치는 모양**이 되므로 임의로
정하지 않고 형님께 여쭀고, *"컨텍스트로 옮기고 가드 목록을 줄인다"* 로 확정.

## 새 형태

| | |
|---|---|
| `backend/workflow/serialization/run_views.py` | 응답 모양 9종 (← `_schemas.py`, `git mv`) |
| `backend/workflow/application/run_detail.py` | 페이로드 매퍼 + 타임라인 + **`build_run_detail`** (← `_helpers.py` + 라우트 본문) |
| `backend/api/v1/runs/detail.py` | **61줄 얇은 어댑터** (이전 187줄) — DI 꺼내고 `None`→404 |
| `backend/mcp/tools/workflow_tools.py` | `bsvibe_runs_detail` 등록 |

`build_run_detail` 은 워크스페이스 밖/미상 런에 **`None`** 을 돌려준다. "여기 없다"를
어떻게 쓸지는 호출자가 정한다 — REST 는 404, MCP 는 `ToolError`. 두 표면 다 교차
워크스페이스와 미상 id 를 구분하지 않으므로 **누출이 없다.**

⚠️ **규칙을 MCP 로 복제하지 않았다.** 복제하면 가드는 초록이 되면서 가드가 막으려던
드리프트를 정확히 만든다(핸드오프 경고). `lint-imports` **5 kept · 0 broken —
새 예외 0건.**

## 가드

RED 를 먼저 봤다:

| 테스트 | RED 일 때 |
|---|---|
| `test_every_rest_route_has_an_mcp_twin` (핀 제거) | `{'GET /runs/{run_id}/detail': 'bsvibe_runs_detail'}` |
| `test_runs_detail_returns_the_same_derivation_the_browser_gets` | `unknown tool: bsvibe_runs_detail` |
| `test_runs_detail_other_workspace_is_not_found` | (아래 함정 참조) |

⚠️ **세 번째가 처음엔 거짓 초록이었다.** `pytest.raises(ToolError)` 는 *"unknown
tool"* 로도 만족된다 — 툴이 **없어서** 통과했다. 부재가 흉내낼 수 없도록
`str(run_id) in str(err.value)` 와 `"unknown tool" not in …` 으로 못박았다.

⚠️ 첫 번째 테스트는 **행이 아니라 빌더가 유도하는 필드**(`trigger.intent_text`,
free-form payload 에서 방어적으로 읽는 값)를 본다. 행이 이미 들고 있는 컬럼을
검사하면 **미러된 툴도 통과한다** — 드리프트가 생길 자리가 바로 유도 쪽이다.

## 체크리스트

- [ ] `uv run pytest tests/mcp/ tests/api/ tests/glue/ -q` → green
- [ ] `uv run lint-imports` → **5 kept, 0 broken** (새 예외 0건)
- [ ] `uv run mypy backend/` → clean
- [ ] `uv run ruff check backend/ tests/ bsvibe_sdk/ plugin/` (CI 인자 그대로)
- [ ] `_KNOWN_GAPS` 가 **3개로 줄었다** (양방향 fail-closed — 세지 말고 읽어라)
- [ ] 배포 후 prod 실측: `bsvibe_runs_detail` 이 브라우저 run 상세와 같은 값을 준다
