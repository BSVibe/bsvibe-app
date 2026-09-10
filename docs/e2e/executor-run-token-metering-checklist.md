# E2E — 워커 실행 런도 토큰을 잰다 (게이트 1 후속)

#911 이 런별 토큰 미터(`execution_runs.usage_*`)와 런어웨이 천장
(`agent_max_run_tokens`, 2M → `run_token_cap_reached` Decision)을 넣었지만,
그 계측은 **네이티브 LiteLLM 턴 하나에만** 배선돼 있었다.

등록형 호스트 워커가 실행하는 런(코딩 에이전트 CLI — `claude_code` / `codex` /
`opencode`)은 체인의 **네 링크가 전부 비어 있어** 영원히 0 토큰을 기록했고,
따라서 **천장이 한 번도 발화할 수 없었다** — 정확히 그 천장이 존재하는 이유인
런어웨이(CLI 에이전트가 48 work 턴을 도는 것)에.

감사 §Ⅱ 가 *"신규 테넌트는 자기 워커를 등록하기 전까지 executor 용량이 0"* 이라
했으므로, **파운더의 네이티브 런을 뺀 모든 워크스페이스에서 천장은 inert** 였다.

**끊어져 있던 네 링크**

| # | 자리 | 증상 |
|---|---|---|
| 1 | 워커 executor 3종 | CLI 스트림이 이미 흘리는 usage 를 파싱 0건 |
| 2 | `executor_tasks` | usage 컬럼 없음 |
| 3 | `POST /workers/result` (`extra="forbid"`) | 받을 필드 없음 |
| 4 | `ExecutorAdapter.chat` | `ChatResponse(content=…)` → usage 기본값 **0**, 그리고 `loop_llm` 의 `getattr(…, 0)` 이 **부재를 측정치로 세탁** |

**이 체크리스트가 걷는 것**: 유닛은 각 링크를 따로 핀으로 박는다. 여기서는
**끊어진 링크가 하나라도 남으면 초록이 될 수 없는 명제** — executor 턴의 토큰이
런 미터에 도달하는가 — 를 확인하고, 배포 후 **실제 워커 런**에서 0 이 아닌 값이
기록되는지를 본다.

## 배포 전 (로컬)

- [x] `uv run pytest tests/executors/test_worker_token_usage.py -q` → **11 passed**
- [x] 절단 실증 ①(어댑터 홉) — `_chat_response_from_task` 를 옛 인라인 형태
      (`ChatResponse(content=task.output or "")`)로 되돌리자 **정확히 2개**가
      빨갛고 나머지 9개는 초록. 증상이 원래 결함과 동일한 `usage_prompt_tokens=0`
- [x] 절단 실증 ②(워커 드레인 홉) — 청크 usage 누적을 지우자 **정확히 1개**가
      빨갛고 `executor_reported_no_token_usage` 경고가 발화. 나머지 10개는 초록
- [x] 양성 대조군 — 기존 테스트 더블 5개(`_StubCompletedTask` · `_Completed`)가
      `AttributeError: usage_prompt_tokens` 로 깨졌다. **어댑터가 새 필드를 실제로
      읽는다는 증거**이지 회귀가 아니다. 더블을 실제 행 계약에 맞춰 수정
- [x] `uv run ruff check backend/ tests/` → All checks passed
      (⚠ `_run_once` 가 PLR0912/PLR0915 를 넘어서 `noqa` 대신 `_scrape_event`
      헬퍼로 분리 — 이 파일이 이미 쓰는 hygiene-split idiom)
- [x] `uv run ruff format --check backend/ tests/` → 1333 files already formatted
- [x] `uv run mypy backend/` → Success: no issues found in 586 source files
- [x] **프로브 PG** (`pgvector/pgvector:pg16`, 포트 15501) — `alembic upgrade head`
      가 빈 DB 에서 완주. `executor_tasks.usage_{prompt,completion}_tokens` 가
      `bigint NOT NULL DEFAULT 0` 으로 실재. **음성 대조군**: 존재하면 안 되는
      `usage_bogus_tokens` 는 0행
- [x] 프로브 PG 전체 스위트 — **6144 passed, 1 skipped** (skip 이 44 → 1 로 떨어짐;
      남은 1건은 Redis 미기동). 단일 실패
      `test_rls_is_active_layer3_for_the_runtime_role` 는 **프로브 아티팩트**로
      확정 — 실패 단언이 `rolsuper OR rolbypassrls` 전제이고 프로브의 `bsvibe`
      역할은 superuser(`t`). CI 는 2역할 셋업이라 통과한다

## 배포 후 (prod)

⚠️ 컨테이너 인터프리터는 `/app/.venv/bin/python` 이다.
⚠️ 워커는 autodeploy 대상이 아니다 — 배포 후
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 필수.
**워커를 재시작하지 않으면 링크 1·3 이 옛 코드라 이 체크는 전부 0 으로 나온다.**

- [ ] prod 컨테이너 갱신 확인 — `StartedAt` 이 이 배포 이후
- [ ] 배포된 코드에 신규 표현식 **있음** / 옛 표현식 **없음** (양성·음성 대조군)
      — `_chat_response_from_task` 존재 · `ChatResponse(content=completed.output`
      부재를 배포본 소스에서
- [ ] 마이그레이션이 prod DB 에 적용됨 — `executor_tasks` 에 두 컬럼 실재
- [ ] 워커 3개 재시작 확인 — 프로세스 시작 시각이 kickstart 이후
      (낡음의 유일한 신호는 **프로세스 시작 시각**)
- [ ] **워커 런 1건을 실제로 돌려** `executor_tasks.usage_prompt_tokens > 0` 인
      행이 생기는지 확인. 0 만 나오면 링크가 아직 끊겨 있는 것이다
- [ ] 같은 런의 `execution_runs.usage_prompt_tokens > 0` — 체인이 미터까지 닿았다는
      최종 증거 (**이 항목이 이 PR 의 존재 이유다**)
- [ ] 워커 로그에 `executor_reported_no_token_usage` 경고가 **없음** — 있으면 그
      executor 의 CLI 가 usage 를 안 흘린다는 뜻이고, 그 executor 는 아직 미계측

## 남은 것 (이 PR 밖)

- 천장 2M 은 데이터 없이 정한 안전천장이다. **이제 실측이 쌓이므로** 튜닝은
  워커 런 토큰 분포를 본 뒤에 (감사 게이트 1 후속).
- 워크스페이스별 토큰 예산(런별이 아닌)은 여전히 없다 — 게이트 4 과금과 함께.
