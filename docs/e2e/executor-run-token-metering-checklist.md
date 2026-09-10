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

## 배포 후 (prod) — 전부 확인 (2026-09-10)

⚠️ 컨테이너 인터프리터는 `/app/.venv/bin/python` 이다.
⚠️ 워커는 autodeploy 대상이 아니다 —
`launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}` 필수.
**워커를 재시작하지 않으면 링크 1·3 이 옛 코드라 이 체크는 전부 0 으로 나온다.**

- [x] prod 컨테이너 갱신 — `StartedAt=2026-09-10T09:22:13Z`, 배포 `0672d7a`
- [x] 배포된 코드 양성·음성 대조군 — **메커니즘으로** 잰다: 호출 지점
      `ExecutorAdapter._chat_with_session` 안에
      `return _chat_response_from_task(completed)` **있음**,
      `ChatResponse(content=completed.output` **없음**.
      ⚠️ 첫 판본은 모듈 전체를 문자열로 훑어 **거짓 경보**를 냈다 — 새 함수의
      독스트링이 옛 코드를 *산문으로 인용*하고 있어서다. 어휘 가드는 메커니즘이
      아니라 텍스트를 문다
- [x] 배포본에서 **실행** — 컨테이너 안에서 `_chat_response_from_task` 를 직접
      호출해 usage `1234/56` 이 그대로 실리는 것을 확인(정적 grep 보다 강한 증거)
- [x] 배포본 ORM 이 컬럼을 안다 — 둘 다 True, 음성 대조군 `usage_bogus_tokens` False
- [x] prod DB 에 컬럼 실재 — `bigint NOT NULL DEFAULT '0'::bigint` 2개,
      `alembic_version = executor_task_tokens`
- [x] 워커 3개 재시작 — 셋 다 프로세스 시작 시각 `18:24:10 KST`(배포 `18:22:11`
      이후). `mac-mini-e2e` 하트비트 `09:25:42Z` fresh
- [x] **워커 런 1건 실행** — direct 런이 `mac-mini-e2e` 로 dispatch,
      `executor_tasks` 3건이 usage>0 으로 종료
- [x] **런 미터까지 도달 — 산술이 맞는다**

      | | prompt | completion |
      |---|---|---|
      | task `d091da2b` | 14,105 | 11 |
      | task `53bb91a1` | 29,082 | 647 |
      | **합** | **43,187** | **658** |
      | **run `70ff629a`** | **43,187** | **658** |

      런 미터가 자기 executor 태스크들의 보고 usage 의 **정확한 합**이다.
      (세 번째 태스크 `e70c872d` 1412/180 은 `run_id=None` 인 chat 형태 프레임
      턴이라 런에 안 묶인다 — 스키마 독스트링과 일치)
- [x] **모집단 음성 대조군** — 종료된 `executor_tasks` **6,826건 중 usage>0 은 3건**
      뿐이고 셋 다 배포 후 몇 분 사이. 나머지 6,823건은 전부 0
- [x] 워커 로그에 `executor_reported_no_token_usage` 경고 없음 —
      `claude_code` 는 usage 를 보고한다

## 남은 것 (이 PR 밖)

- 천장 2M 은 데이터 없이 정한 안전천장이다. **첫 실측이 나왔다**: 사소한
  direct 런 하나가 **43,187 토큰**(2 턴)을 썼다 — 2M 은 그런 런 **약 46회**분이다.
  48 work 턴을 도는 진짜 코딩 런은 훨씬 클 것이므로, 튜닝은 분포가 쌓인 뒤에.
- 워크스페이스별 토큰 예산(런별이 아닌)은 여전히 없다 — 게이트 4 과금과 함께.
