# BSVibe 세션 인수인계 — 2026-09-28 (오후)

**배포 지형** — 둘이 다르다. 헷갈리면 "배포했는데 왜 안 바뀌지"로 한 시간 쓴다.

| | 어떻게 나가나 |
|---|---|
| **컨테이너 스택**(backend·worker) | ✅ **autodeploy 가 2분마다** 올린다 — `_infra/scripts/autodeploy.sh` 의 **별도 블록**(171행~). `PROJECTS` 배열엔 없다(그거 보고 "안 된다"로 오판하기 쉽다). 상태: `_infra/logs/bsvibe-app.deployed` |
| **PWA** | Vercel 이 main 머지에 자동 배포 |
| **호스트 워커 2대** | 🔴 **autodeploy 안 된다.** `main` 을 `git pull` 하고 `launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` 후 **프로세스 시작 시각**으로 확인. plist 를 고쳤으면 `bootout`+`bootstrap` |

⚠️ **프론트와 백엔드를 한 PR 에 담으면 창이 생긴다** — Vercel 은 즉시, 백엔드는 폴러가 2분마다. 09-28 오전 #1061 이 실제로 그랬다(검색창은 살아 있는데 엔드포인트는 아직 없음).

**열린 작업은 이 문서가 아니라 GitHub 이슈에 있다.**

> 🧭 **이 헤더에 전이적인 것을 적지 마라.** 자기 자신을 가리키는 필드는 머지하는
> 순간 거짓이 된다 — 값을 갱신하는 걸로는 못 고치고 **필드를 없애야** 고쳐진다.

---

## §0 — 오늘 한 줄: **결정을 받기 전에 전제를 재라 — 틀린 전제 위의 결정은 사람을 두 번 부른다**

형님이 #959 방향을 골랐는데 **그 선택지의 근거가 내가 적은 틀린 문장**이었다
(*"강제 표를 직접 집는 건 agent 워커의 `requests` 하나뿐"*). 착수하며 런타임으로 세니 **11곳**이었고,
고른 방향은 그중 2곳만 덮었다. 정정을 이슈에 올리고 **다시 물었다** — 형님 시간을 두 번 썼다.

그리고 오후의 두 번째 발견도 같은 모양이다: 하네스의 `harness_worker_recorded` 는 *"기록했다"* 가 아니라
*"record_result 를 불렀다"* 를 쟀다. **센서가 호출을 재고 결과를 안 쟀다** — 그래서 #950 의 판정표가
*"기록이 커밋됐는데"* 라는 틀린 전제를 품고 있었다.

### 이 세션의 산출물

| | |
|---|---|
| **머지 2건** | **#1064**(#959 ① 교차테넌트 읽기 GUC) · **#1065**(#950 publish-before-commit 레이스) |
| **닫힌 축** | #950 체크리스트 마지막 세 칸 — 기다리던 CI 빨강이 와서 **한 번의 조회로** 판정 |
| **결정** | #959: 「클레임 표를 RLS 밖으로」 → **철회**, 「명시적 교차테넌트 읽기 GUC」 로 재결정 |
| **배포 확인** | `932a248` 21:36 · `766bb8e` 22:01 — 정책·컨테이너 코드·health 실측 |
| **런북** | `docs/runbook/local-rls-verification.md` 에 **블라인드 접근을 런타임으로 세는 법**(프로브 + 발행 지점 태그) |

**모델 토큰**: 0. 일회용 PG · 코드 · CI 아티팩트 · prod 읽기 전용.

## §Ⅰ — prod 에서 확인한 것

| | 결과 |
|---|---|
| `alembic_version` | `rls_cross_tenant_read` |
| 정책 | **6/6** 이 `USING` 에만 `'*'`, `WITH CHECK` 엔 없다 |
| RLS 에러(배포 후) | backend·worker **0** — 센서 유효성: 같은 창에 로그 77·57줄 |
| 클레임이 여전히 흐르나 | ⚪ **판정 불가** — 배포 이후 run·request **0건**(직전 24h 도 1건) |
| #1065 코드 | 도는 backend·worker 컨테이너 둘 다에 존재, `/api/health` 200 |
| 호스트 워커 | **재기동 불필요** — 두 PR 모두 `backend/executors/worker/` 를 안 건드리고, 워커는 `dispatch` 를 import 하지 않는다 |

## §Ⅱ — #959: 배경 워커의 블라인드 접근 11곳 (다음 PR ② 의 작업 목록)

측정: 정책에 **빈 GUC 를 기록하는 프로브**(판정은 fail-open 그대로) + SQL 마다 **발행 지점·진입점 주석**.
워커 관련 83파일 + `tests/production`, 895 passed, 런 전후 프로브 6/6. 절차는 런북.

| 종류 | 진입점 → 접근 | ② 에서 할 일 |
|---|---|---|
| 큐 클레임 | `claim_once` → requests SELECT/UPDATE · `_claim_runs_for_drive` → execution_runs UPDATE | `cross_tenant_read()` 로 id·workspace_id 읽기 → **행마다 `workspace_scope` 에서 UPDATE** |
| 블라인드 **쓰기** 🚨 | `claim_once` 안의 `open_run` INSERT · `intake_worker.drain_once` requests INSERT | 스코프 안으로. **intake 는 스코프가 DB 까지 안 닿는다** — 세션이 루프 밖에서 열려 `after_begin` 이 빈 GUC 로 무장됨(09-18 과 같은 모양) |
| 전역 스윕 | `_reap_stale_claims` · `reap_terminal_run_workspaces` · `drain_queued_answers`(`_tick`) | `cross_tenant_read()` + 쓰기는 행마다 스코프 |
| 테넌트 열거(`workspaces`) | `daily_brief._active_workspaces` · `list_with_audit_retention`(schedule) · `auth_dependency._announce` · `settle._resolve_workspaces` | `cross_tenant_read()` |

⚠️ **`cross_tenant_read()` 는 그 안에서 BEGIN 한 트랜잭션만 덮는다** — 이미 열린 트랜잭션은 시작 때 GUC 를 유지.
세션을 루프 밖에서 여는 곳은 이걸로 안 된다(위 intake 와 같은 함정).
⚠️ **UPDATE 는 `'*'` 로 안 된다** — `WITH CHECK` 가 행의 워크스페이스를 요구한다. 클레임의
`UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED)` 는 모양을 바꿔야 한다. SKIP LOCKED 의
다중 워커 안전성을 유지하는 모양인지 **테스트로 박아라**(`test_drive_failure_reaches_the_founder::test_two_workers_share_one_bound` 가 이미 있다).

**미분류**: API/MCP 진입점(`middleware.__call__`·`call_tool`)에서도 블라인드가 찍혔다. 테스트의 auth
override 탓일 가능성이 높아 11 에서 뺐다 — **확인 안 했다.**

## §Ⅲ — #950 이 닫힌 방식 (publish-before-commit)

기다리던 빨강은 #1064 의 CI 에서 왔다(`test_list_dir_returns_entries`, 90s, `last status 'dispatched'`).

| 태스크 `96fdca42` | |
|---|---|
| `harness_worker_saw_task` | +2ms |
| `harness_worker_recorded` | +9ms, `verdict=worker_kept_up` |
| `executor_task_result_recorded` | **없음** (성공 태스크엔 있다) |
| `record_result` 거절 warning 2종 | **없음** ⇒ 남은 출구 = 무로그 `task is None` |

`dispatch_task` 가 **xadd 를 먼저** 하고 커밋은 호출자 몫이었다. 호출자 둘 — `dispatch/adapter.py`(prod 의
모든 executor 턴) · `client_worker_manager.py`(검증 게이트) — 다 같은 순서. 고친 것: 커밋 후 publish,
publish 실패 시 `failed` 로 닫기, 없는 태스크 결과 로그, 하네스가 반환값 단언.

**prod 에서도 났었나 — 미측정.** #965 재전송이 가리고 있었을 수 있다. 센서: `executor_task_result_for_unknown_task`
가 **0 으로 유지되는지**, 재전송 빈도가 줄었는지(§Ⅴ.2).

## §Ⅳ — 형님만 할 수 있는 것

1. **🚨 자격증명 로테이션 — 세 건. 한 유지보수 창에서 묶어라** (09-24 에서 이월)
   * `admin@bsvibe.dev` 비밀번호 (09-23 노출, **라이브 E2E 가 매일 쓰는 실계정**)
   * 워커 토큰 (`docs/runbook/secret-rotation.md` §2b)
   * **prod Postgres `bsvibe` 역할 비밀번호** (09-24 출력 노출). 반경: prod PG 는 **호스트 포트를 안 연다**
2. **그룹방 만들고 바인딩 걸기** (#1047) · **#937 재부팅 cold-boot**(sudo) · **Supabase 가입 정책**

## §Ⅴ — 다음 세션이 할 것

> 🧭 **먼저 재라.** 세 세션 연속으로 *"할 일"* 이나 *"전제"* 가 실측과 달랐다(09-24 · 09-28 오전 #964 · 오늘 #959).

1. **#959 ② — §Ⅱ 의 11곳을 `cross_tenant_read()` 로.** 순서 제안: 블라인드 쓰기 2개(별건 버그, 작다) →
   열거 4개(읽기만) → 스윕 3개 → 클레임 2개(모양 변경, #950 판정표와 닿는다). **PR 을 쌓지 말고 하나씩 머지**
   (squash 가 뒤 PR 을 DIRTY 로 만든다). 끝나면 **fail-closed 전수**(③) — 그때 픽스처 108건이 GUC 없이 INSERT 하는 문제도 같이
2. **#1064 의 판정 불가 칸** — 다음 실제 런이 생기면 클레임됐는지(`claimed_by` / `open` 이탈) 한 줄로 확인해 #959 에 적어라
3. **#1065 의 prod 센서** — 며칠 뒤 `executor_task_result_for_unknown_task` 건수와 재전송 로그 빈도. 0 이 아니면 레이스가 prod 에서도 났다는 뜻이고, **0 이면 생산자가 켜져 있는지부터**
4. **#1042 잔여** — 공통 여백/리듬(설정 탭 「위험 구역」) · 활동 탭 데이터 모양(같은 분 27행 · 제목 없는 행). 09-24 코멘트에 캡처
5. **#954** 미계상 토큰 · **#949** 파리티 4건 · **#957** key-id
6. 잡일: PAT 라우터를 다시 v1 게이트 안으로 · 워커 plist PATH 1.17.3 고정 되돌리기

### 별건 후보 (이월 + 오늘)

* 🆕 **`record_result` 의 무로그 `return None` 은 고쳤지만, `/api/v1/workers/result` 라우트가 None 을 뭘로 돌려주는지 안 봤다** — 200 이면 워커는 성공으로 안다([[a-timeout-names-the-waiter-not-the-cause-read-the-other-end]] 모양)
* 🆕 **`tests/data/test_rls_pg.py` 가 스키마를 DROP 하고 마이그레이션을 다시 돈다** — 공유 PG 로 RLS 실험하는 사람의 조건을 원복한다(09-28 오전 · 오후 두 번 걸렸다)
* **`delivery_events` 테이블이 0행** — 스키마도 인덱스도 있는데 **쓰는 코드가 없다**
* **거부 경로가 런을 안 닫는다** — 승인은 런을 해소하는데 `queue_cleanup` 거부는 큐만 지운다
* **`executor_workers` 에 3~108일 묵은 행 6개가 아직 `status='online'`**
* **바인딩 폼의 커넥터 라벨이 비대칭** — `github — blas1n/…` 는 외부 참조를 보이는데 `telegram` 은 이름뿐
* ⚠️ **`~/.claude/.../MEMORY.md` 가 한도(24,576B) 코앞이다.** 줄을 더하려면 **먼저** 해소된 항목을 `MEMORY_ARCHIVE.md` 로 내려라

### 검증 안 된 것 (정직하게)

* **fail-closed 전수는 아직 안 돌렸다** — 오늘 잰 건 fail-open + 프로브(블라인드 접근의 **목록**)이지, 닫았을 때 무엇이 깨지는지가 아니다
* 블라인드 목록의 **코퍼스는 워커 관련 83파일**이다. 테스트가 안 밟는 워커 경로는 목록에 없다
* API/MCP 진입점의 블라인드(§Ⅱ 미분류) · publish-before-commit 의 prod 발생 여부(§Ⅲ)
* 09-28 오전 이월: 그룹방 승인 동작 · 금고 세션 만료 임계값 21/30(추정) · 09-21·09-18 이월분 그대로

## §Ⅵ — 이 세션에서 값을 한 규율

* **🧭⭐⭐⭐ 선택지를 내기 전에 그 선택지를 가르는 전제를 재라.** 인수인계 문장을 전제로 AskUserQuestion 을 냈고,
  형님이 골랐고, 착수 첫 시간에 전제가 무너졌다. **"대가가 X 개 지점에 비례한다"** 는 옵션이면 X 부터 세라
* **🔬⭐⭐⭐ 센서가 호출을 재는지 결과를 재는지 확인하라.** `harness_worker_recorded` 는 호출 뒤에 찍혔고
  반환값(None)을 안 봤다. 무로그 `return None` 과 합쳐져 판정표의 마지막 칸을 가렸다
* **⏱⭐⭐⭐ 긴 런엔 출력 정지 감시를 붙여라.** 프로브가 테스트를 교착시켰는데 감시가 없어 **4시간** 몰랐다.
  `stat -f %m <출력>` 이 N 초 안 바뀌면 `pg_stat_activity` 를 찍고 죽이는 루프 한 줄이면 된다
* **🪤⭐⭐ 측정 장치가 측정 대상을 막을 수 있다.** 프로브의 `ON CONFLICT DO UPDATE` 카운터가 행 락 경합 → 교착.
  계측은 **경합 없는 append-only** 로
* **🐚⭐⭐ zsh 함정 두 개가 또 나왔다.** `$F` 는 단어 분할이 안 된다(`${=F}`), 변수에 담은 명령은 실행 안 된다.
  그리고 **셸 함수 이름을 `cut` 으로 지어 시스템 명령을 가렸다** → 무한 재귀, 소스가 절단 상태로 남을 뻔했다.
  전선 절단은 **스크립트 파일**로(`mktemp` 백업 + 끝에서 복원 + `cmp` 확인)
* **🧪⭐⭐ 격리 수준을 증명하려면 연결이 진짜로 갈라져야 한다.** publish 시점 가시성 테스트는 `memory_session`
  (공유 연결)이면 미커밋 행이 보여 아무것도 증명 못 한다 — `shared_file_sessionmaker` 로
* **🚀⭐ 런처가 다르면 결과가 다르다.** `.venv/bin/pytest` 로 돌리면 `test_import_contracts` 2건이 빨갛고 `uv run` 이면 초록 — CI 와 같은 런처로
* **🧯 배포 디렉터리에 커밋하지 않았다.** 세 PR 전부 워크트리에서 나왔다
