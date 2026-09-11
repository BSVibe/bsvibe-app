# BSVibe 다중 사용자 준비도 감사 — 2026-09-10

**질문**: *"혼자 쓰는 것과 스케일이 다르다 — 다른 사용자에게 열 수 있는가."*
**방법**: 코드 감사 에이전트 4 + 심화 서브감사 3(MCP/라우트 · 자격증명 · 파일시스템)
+ **prod 직접 실측**(DB 카탈로그 · 인프라 · launchd · 백업 로그). High 판정 전건과
판을 가르는 주장은 **메인 세션이 코드 본문으로 재측정**했다(✅ 표시). 기준 커밋
`4b4b655`, 측정 2026-09-09 저녁 ~ 09-10 새벽.

> ⚠️ 이 문서의 주장은 측정 시점의 것이다. 실행 전 반드시 해당 파일:줄을 다시 열어라.
> [INFERRED] 표시는 코드로 추론했으나 라이브 재현은 안 한 것.

---

## 종합 판정

**아니오 — 지금 열면 안 된다.** 그리고 이유의 무게중심은 "미완성 기능"이 아니라
**두 번째 사용자가 생기는 순간 성립하는 공격 표면과 공유 병목**이다.

핵심 구조 인식 세 가지:

1. **격리의 실질은 3층이 아니라 1.x층이고, 백그라운드에서는 0.x층이다.**
   PG RLS(3층)는 45개 테이블 중 **6개**, 그마저 GUC 미설정 시 **fail-open**.
   2층(ORM 필터)은 32개 모델을 덮지만 **SELECT 한정 + 요청 경로 한정** — 백그라운드
   워커 전체(스케줄러·스윕·AgentWorker·릴레이)는 contextvar/GUC 설정 0건이라
   2층·3층이 동시에 꺼진 채 돌며, 격리는 **수동 WHERE 단 한 층**이다. 그 수동
   검사가 빠진 지점이 실제로 발견됐다(H1).
2. **플랫폼 전체의 네이티브 런 동시성이 1 이다.** AgentWorker 가 claim 배치를
   `for … await` 로 한 런씩 terminal 까지 몰고 간다(✅). 두 번째 사용자의 첫 경험은
   "형님의 1시간 런이 끝날 때까지 내 런이 시작 안 됨"이다.
3. **비용 통제가 우연히 그 병목과 같은 객체다.** 금액·토큰·호출수 상한이 코드에
   0건(✅ 스키마 실측 포함)이라, 실효 지출 상한은 직렬 처리량뿐이다.
   **병목을 고치는 순간 비용이 무방비가 된다 — 수리 순서가 강제된다.**

### 사용자 N 에서 부러지는 순서

| N | 부러지는 것 |
|---|---|
| **2** | 보안 High 4건의 공격 표면 성립 · 직렬 드라이브 공유(기아) · 가입/워커 CLI 경로 부재로 애초에 못 들어옴 |
| **10** | 전역 FIFO 기아 심화 · verify 슬롯 전역 직렬(✅) · 파운더 수동 개입 지점 6곳 × N · cap 우회 런 mint |
| **100** | 단일 Mac mini(컴퓨트·디스크·회선) · 수평확장 차단 2곳(인프로세스 샌드박스 상태, `var/` 머신로컬) |

---

## Ⅰ. 보안 — 다중 사용자 개방을 막는 결함

### 치명/High (전건 ✅ 메인 재측정 확정)

| # | 결함 | 위치 | 성격 |
|---|---|---|---|
| **H1** | `POST /api/v1/workers/result` 가 body 의 `task_id` 를 **소유 워커·워크스페이스·상태 검사 없이** 닫는다. 코드가 스스로 적음: `_ = worker  # auth only`. 아무 워커 토큰(무료 사용자가 자기 워크스페이스에 등록한 것 포함)으로 남의 태스크를 임의 `output` 으로 종료 → 그 output 을 **상대 워크스페이스의 에이전트 루프가 결과로 소비** | `api/v1/workers.py:338` → `executors/dispatch.py:558-563` | 교차 테넌트 **콘텐츠 주입 + 런 DoS**. task_id 는 로그·스트림에 흐르는 값 |
| **H2** | unclaimed OAuth 설치 풀이 **전역** — `list_unclaimed(session)` 에 워크스페이스 인자 자체가 없고, `claim` 은 PK 로만 조회. 테넌트 A 가 B 의 Sentry 설치를 목록에서 보고 먼저 claim → **B 의 액세스 토큰이 A 에 바인딩**. REST·MCP 쌍둥이 동일. 생산자는 무인증 콜백(Sentry 가 state 를 안 넘김) | `connectors/auth/store.py:199-217`, `service.py:190-233`, `api/v1/connector_oauth.py:261-283`, `mcp/tools/connectors_tools.py:508-538` | 교차 테넌트 **자격증명 탈취**(선착순 레이스). 에이전트 2개가 독립 발견 |
| **H3** | **배포 전역** OAuth 앱 자격증명(slack/notion/discord/sentry 의 client_secret) 쓰기에 **role 게이트 없음** — `Depends(get_current_user)` 뿐. 아무 워크스페이스의 아무 멤버가 인스턴스 전체의 OAuth 앱을 자기 것으로 교체 가능 = OAuth 피싱 피벗 | `api/v1/connector_oauth.py:186-211` + MCP 쌍둥이 `connectors_tools.py:471-484` (`require_role` 는 `deps.py:213` 에 있는데 미적용) | 테넌트→인스턴스 권한 상승. 에이전트 2개가 독립 발견 |
| **H4** | 텔레그램 봇 토큰이 URL 경로에 들어가고(`bot{token}/{method}`), `raise_for_status()` 의 httpx 예외 메시지가 URL 전체를 담으며, `PluginRunner._call` 이 `str(exc)` 를 **structlog + PluginRunError 재포장**으로 퍼뜨림 → 로그 · `ActionResult`(런 산출물) · **502 응답 본문**까지 4개 싱크. 봇 토큰 = 커넥터 signing secret 평문 | `plugin/telegram/client.py:78,87` → `extensions/plugin/runner.py:121` → `connector_dispatch/__init__.py:142-150,255-268` · `api/v1/connectors.py:642-645` | 자격증명 로그 유출. 수리 지점 명확: 리포에 이미 있는 `scrub_token` 을 러너 경계에 |

### Medium — 개방 전 처리

* **viewer 가 `safe_mode` 를 끌 수 있다** (✅) — 워크스페이스 PATCH/DELETE 가 멤버십만
  검사하고 role 검사 **0건**(`api/v1/workspaces.py:60-77,122-157`). MCP 는 같은 연산이
  `mcp:admin` 뒤 — **브라우저가 에이전트보다 느슨한 역전**. viewer 가 테넌트 전체의
  배송 승인 게이트를 해제하거나 워크스페이스를 soft-delete 할 수 있다.
* **RLS 실태** (✅ prod 카탈로그 실측) — 6/45 테이블, 정책은 GUC 미설정/빈값 시
  fail-open(`20260612_gdpr_l1_and_rls.py:97-103`). `executor_tasks`·`connector_accounts`·
  `oauth_access_tokens`·`safe_mode_queue_items`·`memberships` 미포함.
* **[INFERRED] GUC 가 풀 커넥션에 잔류** — `set_workspace_guc` 가 `is_local=false` 인데
  미들웨어는 contextvar 만 리셋(`data/rls.py:39-46` vs `api/middleware.py:31`). 공개
  라우트(웹훅 등)가 직전 요청의 GUC 를 물려받으면 RLS-강제 테이블 INSERT 가
  간헐 실패("플레이키한 커넥터"로 위장). **프로브 PG 로 재현 테스트 1개 값어치.**
* 무인증 `/introspect` 가 `workspace_id` 노출 · `device_authorization` 이 미검증
  `client_id` 로 무제한 행 생성(피싱 표면) · SSE 가 세션 JWT 를 쿼리스트링으로 ·
  로그인/리셋 rate limit 없음(인프로세스 limiter 는 DCR 한 곳뿐).
* 자격증명 저장: `model_accounts`·OAuth 토큰은 AES-256-GCM ✓(prod 실측 컬럼 확인),
  그러나 **`delivery_config['webhook_secret']` 는 같은 행의 암호화 컬럼 옆에 평문** ·
  응답 마스킹이 3-키 **denylist** 라 새 키는 그대로 유출(오늘 기준 trello `api_key`) ·
  pending/unclaimed 행 **reaper 없음**("TTL-reaped" 독스트링은 거짓) · soft revoke 가
  OAuth 토큰 행을 살려둠 · 단일 정적 키, 로테이션 경로 없음(key-id 컬럼 부재).

### 파일시스템 격리

* **교차 테넌트 탈출은 현 설정에서 막혀 있다** — 도커 경로는 `/work` 만 마운트,
  prod 는 `BSVIBE_SANDBOX_ENABLED=true` 를 `:?` 로 강제. **그러나** `var/runs`·
  `var/products`·`var/vault` 가 워크스페이스 층 없는 **평면 구조**라,
  `sandbox_enabled=false`(유효한 설정값) 한 번이면 전 테넌트 파일이 이웃이 된다.
  네이티브 루프는 이 경우를 거부하지 않는다(MCP 전송로만 거부 ✓).
* **`client_attach` 파일 도구에 트래버설 검증이 없다** — `head -c … -- <경로>` 를
  그대로 실행(`client_worker_manager.py:258-289`). 절대경로·`..` 로 **워커 호스트의
  임의 파일 읽기**(`~/.ssh/id_rsa`). 교차 테넌트는 아니나(자기 워커) *"에이전트는
  주어진 것만 본다"* 원칙 위반이고, noop·docker 세션엔 있는 가드가 여기만 없으며
  테스트도 없다. `write` 는 거부됨 ✓.
* 같은 product 동시 런 2개가 **컨테이너를 재사용하며 낡은 마운트**를 물려받음
  (`docker_manager.py:256-266` — `workspace_path` 무시) + `finally` 의 release 가
  병렬 런의 컨테이너를 철거. 같은 테넌트지만 작업물 유실 결함.
* 잠복: vault storage 의 `startswith` 접두사 매칭(`graph/storage.py:54-58` — 이웃
  `vault.py` 는 `is_relative_to` 로 옳게 함) · `client_workspace_path` 무검증
  (워크스페이스 토큰 필요).
* **견고한 곳**(양성 대조군): MCP 인증 체인(workspace 는 JWT `wsp` 클레임, 어떤 툴도
  `workspace_id` 인자 없음 + `extra="forbid"`), by-id 툴 전수의 소유 재검사, run 토큰
  이중 바인딩, 아티팩트 스토어 중앙 가드, 웹훅 서명/스코프 설계, git 토큰 scrub.

### 2층(ORM auto-filter) 커버리지 — 요청 경로는 견고, 백그라운드는 0층

* **설치는 누락 불가능한 모양** (✓): `data/scoping.py:86-99` 가 모듈 import 시
  Session 클래스 레벨에 자동 등록, `workspace_id` 가 있는 **32개 모델 전부**에
  `with_loader_criteria` 주입. 명시 opt-out 6개(memberships·oauth_* — 설계상),
  컬럼 자체가 없어 불가한 것 7개(자격증명 테이블은 FK 간접 스코프).
* **그러나 SELECT 한정** — `state.is_select` 가 아니면 return(`scoping.py:66`).
  ORM 벌크 `update()`/`delete()` 4곳은 2층 밖(수동 WHERE 의존).
* **백그라운드 전 경로에서 2층·3층이 동시에 no-op** — `set_current_workspace_id`
  프로덕션 호출은 **5곳 전부 요청 측**(MCP·deps·PAT). `workflow/`·`schedule/`·
  `workers/`·`dispatch/` 에는 contextvar·GUC 설정이 **0건**. RLS 는 GUC 미설정 시
  fail-open 이므로, 스케줄러·스윕·릴레이·AgentWorker·settle 등 **모든 백그라운드
  워커의 격리는 수동 WHERE 단 한 층**이다. 양성 대조군: `trust_surface.py` 는 전
  쿼리 수동 필터 ✓. **⇒ 회귀 한 번이 곧 크로스 테넌트.**
* raw SQL 표면은 사실상 깨끗(✓): 테넌트 데이터를 만지는 `text()` 6곳 중 5곳 수동
  필터, 1곳(intent_examples DELETE)은 간접 스코프. `exec_driver_sql`/f-string SQL
  프로덕션 0건.
* 곁가지: `ingest_batches` 테이블 — workspace_id 컬럼은 있는데 **ORM 모델이 리포에
  없고** 유일 참조가 기본 None 인 optional recorder [INFERRED 죽은 테이블].
  `audit_events` 와 같은 모양 — 같은 절차(쓰는 곳+말하는 곳 세기)로 별도 재측정 후
  처분할 것.

---

## Ⅱ. 스케일 — 무엇이 먼저 부러지나

### 동시성 한계 실측표 (전 항목 파일:줄 근거는 에이전트 보고 원문에)

| 한계 | 값 | 스코프 |
|---|---|---|
| **AgentWorker drive** (✅) | **직렬 1** — `for run_id in claimed: await drive(run)` · 병렬 프리미티브 0건 | **전역** |
| run claim | 배치 10, `ORDER BY created_at` 전역 FIFO — 워크스페이스 공정성 없음 | 전역 |
| run cap | 워크스페이스당 3 (NULL=무제한) — **집행은 REST+MCP 2곳뿐**(✅), 스케줄·웹훅·step-spawn 은 **우회 mint** | 워크스페이스 |
| verify slot (✅) | advisory 키가 **슬롯 인덱스만 해시**(`verify_slot_key` 본문 확인) → 슬롯 0 을 전 워크스페이스 공유 = verify 전역 직렬 | 사실상 전역 |
| sandbox 세마포어 | 2, acquire 타임아웃 없음, 인프로세스 | 전역 |
| DB 풀 | 설정 0건 → 기본 5+10/엔진 (현재 사용 8/100 — 여유) | 프로세스 |
| Redis 스트림 | XTRIM/expire **0건** — acked 엔트리 무한 축적 | 전역 |

### 워커-테넌트 결합 — 격리는 양호, 함의가 반전

등록형 호스트 워커는 **워크스페이스에 바인딩**되고 자기 전용 스트림만 폴링한다
(다른 테넌트 코드를 집을 경로 없음 ✓). 뒤집으면: **신규 테넌트는 자기 워커를
등록하기 전까지 executor 용량이 0** — 파운더 플릿은 일반화되지 않는다. 네이티브
(litellm) 런만 서버에서 돌고, 그것이 위의 직렬 병목을 지난다.

### 비용

* 금액·토큰·호출수·일일 한도 **0건** — 스키마 45개 테이블에 cost/spend/usage 컬럼
  0(✅ prod 실측, 대조군: 무관 매치 2건), `ModelAccount` 에 한도 필드 없음,
  `LlmClient.chat` 에 max_tokens/timeout 강제 없음. budget 테이블은 이미 삭제됨.
* `round_budget` 은 **턴 수**(work 48·prepare 3·verify 1·summarize 2 ≈ 런당 54턴).
* 유계 장치는 있음(✓): drive 연속실패 3 → Decision 정지, notify 재시도 5, 스케줄
  최소 1분 granularity. 그러나 스케줄 1개 = 이론상 1,440런/일 × 54턴, cap 미적용.
* **⇒ 수리 순서 강제**: 직렬 병목 해소 *전에* 지출 계측+상한이 먼저 들어가야 한다.

### 남용/트랜잭션/수평확장

* rate limit·요청 크기 제한 미들웨어 **없음**(웹훅 서명 검증은 건재 ✓).
* **DeliveryWorker 가 FOR UPDATE 트랜잭션을 연 채 외부 배송** — 배송 120s 초과 시
  idle-in-tx 가드가 claim 커넥션을 죽여 **중복 PR/댓글**(at-least-once) + poison-pill
  무한 5초 재시도. connector import 는 요청 안 동기 완주(524 표면). RelayWorker 잠복.
* 백엔드 2대 시: 인프로세스 샌드박스 상태(세마포어·레지스트리·orphan sweep 이
  상대 프로세스 컨테이너를 고아 오판) + `var/` 머신로컬이 차단 지점. 그 외 락·큐는
  PG advisory/SKIP LOCKED 로 이미 멀티프로세스 안전(✓).

---

## Ⅲ. 온보딩 — 낯선 사용자는 어디서 막히나

뼈대는 셀프서브다(✓): 첫 로그인에 user+워크스페이스+오너 멤버십+개인 Account 자동
생성, LLM 키 BYO UI, 공개 리포 제품 생성→첫 요청 UI, 빈 화면 3단계 체크리스트.

| 막힘 | 실태 |
|---|---|
| **가입** | 이메일 가입 UI/API **없음**(로그인만) — 신규 진입은 Google/GitHub OAuth 뿐, 허용 여부는 **Supabase 프로젝트 설정(리포 밖)** |
| **워커 CLI** | 등록 UX·토큰 발급은 GitHub-runner 식으로 완성. **CLI 배포 채널 부재** — PyPI publish 워크플로 없음, 안내 명령에 설치 단계 자체가 없음. 현재는 리포 클론 = 파운더 개입 |
| **숨은 계단** | 모델 계정을 만들어도 resolver 가 *"never auto-stamps"* — 기본 계정 미지정 시 첫 런이 `no_model_account` Decision 으로 정지. **체크리스트가 이 단계를 안 알려줌** |
| **모순** | litellm+server_sandbox 면 워커 없이 도는 코드 경로가 있는데 온보딩 UI 는 워커를 필수 2단계로 제시 |
| **업그레이드** | 결제·플랜 표면 0(stripe import 0) — cap 도달 시 외부 pricing 링크, 해제는 **운영자 SQL** |

파운더 수동 개입 지점 전수: Supabase 가입 설정 · 워커 CLI 전달 · GitHub App 1회
셋업 · slack 등 OAuth creds 붙여넣기 · cap 해제 SQL · `SANDBOX_ENABLED` 결정.

---

## Ⅳ. 인프라/운영 (prod 실측 다수)

### 실측으로 좋았던 것

* **백업 견고** (✅ 로그 실증): 매일 03:00 pg_dump → 검증 → 로컬 14일 + **R2 오프박스**,
  최근 3일 연속 "copy OK". 실패 시 `.last-success` 미갱신 → watchdog 26h 임계 페이징.
* **watchdog 이 텔레그램으로 실제 페이징** (✅ env+로그 실증): 2분 주기 — 컨테이너
  생존·백업 신선도·heartbeat·wedged run·디스크(40G warn/20G crit)·고아 프로세스.
* deploy ref guard(브랜치→prod 사고 재발 방지), run-liveness 배포 연기, LLM 장애
  유계(#897 경로), Supabase 정지 **탐지** 워커(AuthDependencyWorker), 컨테이너 5개
  `unless-stopped`(✅), 디스크 여유 364G(✅).

### 구멍

| 항목 | 실태 | 위험 |
|---|---|---|
| **머신 수준 장애의 외부 관측** | 감시자 전원이 같은 Mac mini — 정전·커널패닉·회선이면 **아무것도 모른다**. 박스 밖 모니터 0 | 치명 |
| **재부팅 자동복구 미실증** | colima 기동 유닛이 `_infra/launchd` 에 없고(✅ 5개 전부 다른 것) brew 서비스는 **error/exit 1**(✅). 자동로그인은 설정됨(✅) — 그러나 docker 데몬이 안 뜨면 `unless-stopped` 는 공중에 뜬다 | 치명 |
| Supabase 무료 정지 | 로그인 즉사 + 수분 내 기존 세션 401 전멸(JWKS). 재발 전제(형님 결정: 유료 전환 안 함) — 탐지는 되나 **다중 사용자면 전원 로그아웃** | 높음 |
| **vault(지식 md) 백업 없음** | pg_dump 는 DB 만 — appdata 볼륨 오프박스 백업 0. 지식은 파일이 SoT → **디스크 사망 = 지식 전손** | 높음 |
| 복구 런북 없음 · 복원 리허설 0 | restore 절차 문서 부재. 백업이 *복원되는지* 아무도 확인 안 함 | 높음 |
| migration 실패 = restart loop | `set -eu` → exit → 무한 재시작(전례 있음), 롤백 절차 문서 없음 | 높음 |
| **`deploy/.env.prod.bak-*` 4개 방치** (✅ git status 확인) | gitignore 가 `.bak` 미포함 — `git add -A` 한 번이면 시크릿 커밋 | 높음 |
| 관측 빈약 | 로그 stdout 뿐(컨테이너 재생성마다 유실) · Sentry 0 · `/api/health` 는 DB/Redis 안 보는 무조건 ok | 중간 |
| 라이브 관측 | watchdog 이 현재 **고아 폭주 프로세스 2개** 경보 중 (pid 806 @127% 5h50m+, pid 7758 @67%) — 확인 필요 | — |

---

## ✅ 게이트 0 완료 (2026-09-10) — 보안 High 4건 전부 prod 배포

| PR | 결함 | 수리 | prod |
|---|---|---|---|
| #907 | H1 워커 result 태스크 탈취(교차 테넌트 output 주입) | `record_result` 에 worker_id 필수 바인딩 + dispatched 상태 전제 | ✅ `e4c87ef` 배포·실행검증 |
| #908 | H2 unclaimed OAuth 전역 claim(토큰 탈취) | `installation_ref` 소유 증명 + 전역 목록 제거(REST+MCP) | ✅ `1abb5a2` 배포·실행검증 |
| #909 | H3 전역 OAuth creds 무권한 쓰기 + viewer safe_mode | 런타임 쓰기 제거(env 전용) + `require_role`(단수 admin/복수 admin·owner) | ✅ `a82eb2f` 배포·실행검증 |
| #910 | H4 텔레그램 토큰 로그 유출(4싱크) | 클라이언트 소스 scrub + `from None` 으로 원본 예외 억제 | ✅ `eda3fcf` 배포·실행검증 |

각 PR: TDD(절단 실증 포함) · 전체 스위트 통과 · ruff/format/mypy/import-linter ·
배포된 코드 **실행 검증**(정적 grep 아님) · 호스트 워커 kickstart. 스키마 변경 0.

⚠️ **게이트 0 은 "아무 인증 멤버가 밟는" 표면을 닫았다.** 남은 Medium 이하
(GUC 잔류 · RLS 6/45 · rate limit 부재 · SSE 쿼리토큰 등)와 게이트 1~4(비용 계측 ·
온보딩 물리경로 · 박스 밖 관측 · 과금/수평확장)는 **여전히 열려 있다.**
다중 사용자 개방은 이 게이트들 뒤다.

---

## Ⅴ. 개방 게이트 — 수리 순서 제안

**게이트 0 (코드, 즉시): 보안 High 4 + viewer role.** 전부 수리 모양이 명확하다 —
H1 `WHERE worker_id = :me` + 상태 전제(리포 안 `revoke_pat` 이 이미 그 패턴을
독스트링으로 명시), H2 설치 시 워크스페이스 바인딩(signed state), H3/viewer
`require_role` 적용(이미 존재하는 프리미티브), H4 러너 경계 `scrub_token`.

**✅ 게이트 1 (계측 + 런어웨이 상한) 완료 — #911, prod `3abae11`.** execution_runs 토큰 2컬럼 + `agent_max_run_tokens`(2M, run_token_cap_reached Decision). **남은 게이트 1 후속**: 워크스페이스별 토큰 예산 · cap 을 스케줄/웹훅 경로에도 · 상한값 튜닝(데이터 본 뒤). 순서가 반대면 무방비 구간이 생긴다.
같은 PR 축에서 cap 을 스케줄·웹훅 경로에도 걸어라.

**🔶 게이트 2 (온보딩 물리 경로) — 코드 부분 완료.** ✅워커 CLI curl 설치(#912, `GET /install-worker.sh`) · ✅첫 계정=기본 계정 자동지정(#913). **남은 조각: Supabase 이메일 가입 정책 — 리포 밖 Supabase 콘솔 설정이라 형님 결정 사항(코드 불가).**

**🔶 게이트 3 (가용성) — 코드 가능 전부 완료 (_infra).** ✅vault+skills 오프박스 R2 백업(backup.sh) + 복원 런북 · ✅colima-ensure 에이전트(실패한 부팅 start 재시도 — 감사의 '유닛 없음'은 오진, 실제 갭은 무재시도) · ✅dead-man's-switch 하트비트(박스 밖 머신죽음 감지). **형님 잔여: healthchecks.io 무료 체크 만들어 URL 을 ~/.bsvibe/heartbeat.env 에 · 유지보수 창에 재부팅 cold-boot 테스트.**
백업 + 복원 리허설 1회 + colima 부팅 경로 실증(재부팅 한 번 해보기).

**게이트 4 (개방 규모 결정 후): 과금 + 수평확장** — stripe 류, `var/` 공유화,
샌드박스 상태 외부화, verify slot 키에 워크스페이스 축.

---

## ⚠️ 감사 중 사고 1건 — 시크릿 로테이션 권고

격리 서브에이전트가 DSN 역할 확인 과정에서 **`.env.prod` 의 비밀값 2개를 자기 도구
출력에 노출**시켰다(최종 보고서에는 미포함, 노출 범위는 이 머신의 로컬 세션
트랜스크립트 한정 — 외부 전송 없음). 에이전트 스스로 보고했고 **해당 값 로테이션을
권한다.** 어차피 §Ⅳ 의 "키 로테이션 절차 부재"가 함께 드러났으니, 로테이션 절차를
이번에 만들면서 돌리는 것이 맞다.

## 측정 한계 (안 한 것)

부하 테스트·실제 침투 시도·GUC 잔류 라이브 재현([INFERRED] 그대로)·복원 리허설·
Supabase 설정 확인(리포 밖)·PWA 프론트 자체 감사. 에이전트 발견 중 High 전건과
판 가르는 주장은 재측정했으나 **Medium 이하 다수는 에이전트 측정 그대로**다 —
실행 전 해당 위치 재확인이 규율이다.

---

## 부록 — #904 진단이 처음 발화 (2026-09-10, 게이트 0 작업 중)

인수인계 §Ⅳ.b 가 기다리던 자연 발생이 왔다. **#907(H1) 머지 후 #908(H2)의 CI**
에서 다섯 달 된 flake `test_client_worker_manager.py::test_list_dir_returns_entries`
(외 2개)가 터졌고, **#904 가 심은 진단 숫자가 처음으로 실렸다**:

```
exit None — exec timed out after 90.0s (command budget 30.0s + 60.0s report slack;
            46 polls in 90.0s, last status 'dispatched')
```

§Ⅳ.b 판정표 적용: `polls 46 ≈ timeout_s/2.0(=45)` + `last_status 'dispatched'`
= **"워커가 보고를 안 했다"** (행 가시성도, 러너 기아도, terminal-누락도 아님).

**가드 회귀가 아님을 확정**: H1 의 `record_result` worker_id 가드는 결정적이라
(worker_id 불일치면 타이밍과 무관하게 항상 거부) 회귀면 로컬에서도 빨개진다.
H2 브랜치에서 이 테스트 3개를 5회 반복 → **5/5 통과**. CI 만 빨갛다 = 타이밍 flake.
테스트 자신의 주석도 *"CI flake ... the fake worker didn't receive the exec task
within its budget"* 로 이 하네스 타이밍을 적어놨다.

⇒ **원인이 국소화됐다**: 제품/판별기 갭이 아니라 **fake-worker 하네스의 budget 이
느린 CI 러너에서 exec 태스크 픽업 전에 소진**되는 것. 다섯 달 "미상"에서 처음으로
좁혀진 지점. 수리 후보: `_FAKE_WORKER_BUDGET_S` 상향 또는 fake worker 를 결정적
핸드오프로 재설계. **게이트 0 과 별개 트랙**(형님 판단 필요) — H2 는 이 flake 와
무관하므로 CI 재실행으로 진행.

---

## ✅ 해소됨 (2026-09-11) — `api.bsvibe.dev` 의 데이터센터 IP 403 [Bot Fight Mode]

게이트 3 의 박스 밖 감시(PR #918)를 붙이자마자 **첫 실행이 실패**하면서 드러났다.
감사 본문의 어떤 항목도 이걸 예측하지 못했다 — **박스 밖에서 찔러본 적이 없었기 때문**이다.

### 측정

GitHub Actions 러너(Azure IP `52.179.93.130` · `9.234.151.116`)에서 실측.
대조군은 형님 머신(주거용 IP)이며 **같은 URL 이 200**이다.

| 표면 | 주거용 IP | 데이터센터 IP |
|---|---|---|
| `/api/v1/workers/register` · `/poll` · `/heartbeat` | 200 | **403** `cf-mitigated: challenge` |
| `/mcp` | 200 | **403** |
| `/api/oauth/device_authorization` · `/token` | 200 | **403** |
| `/install-worker.sh` | 200 | **403** |
| `/api/health` · `/redoc` | 200 | **403** |
| `/.well-known/oauth-*` | 200 | **200** (CF 자동 면제) |
| `/api/webhooks/github/…` (기본 curl UA) | — | **403** |
| `/api/webhooks/github/…` + `GitHub-Hookshot` UA | — | **404 — CF 통과, 앱 도달** |
| `/api/webhooks/slack/…` + `Slackbot` UA | — | **403** |
| `bsvibe.dev` (PWA, Vercel 존) | — | 307 (영향 없음) |

브라우저 UA 로도 403 이므로 **UA 단독 기준이 아니라 봇 점수/ASN** 이다. 다만
`GitHub-Hookshot` UA 는 통과하므로 **UA 가 점수에 크게 기여**한다.

### 함의 — 이건 모니터링 문제가 아니라 게이트 2·4 문제다

1. **클라우드 워커는 등록조차 불가.** 감사 §Ⅱ 는 *"신규 테넌트는 자기 워커를 등록하기
   전까지 executor 용량이 0"* 이라고 적었다. 그런데 그 워커를 **클라우드 VM 에 올리면
   `register`·`poll`·`heartbeat` 이 전부 403** 이다. 파운더처럼 집 머신에 올리는
   경우에만 동작한다 — **다중 사용자 전제가 여기서 무너진다.**
2. **온보딩 원라이너가 클라우드/CI 에서 실패.** PR #912 로 복원한
   `curl -fsSL …/install-worker.sh | sh` 가 403.
3. **MCP·device flow 가 클라우드에서 불가.**
4. **인바운드 웹훅은 UA 운에 달려 있다.** GitHub 은 통과, 그 외는 미지수.

### ✅ 텔레그램 인바운드 — 확인 완료 (2026-09-11). **CF 가 아니라 배선 부재였다**

봇 토큰을 복호화해 `getWebhookInfo` 를 직접 호출했다. 결과:

```
url_set: False
pending_update_count: 0
has_custom_certificate: False
last_synchronization_error_date: 2026-09-08T06:10:17Z
```

**`url_set: False` — 텔레그램에 웹훅이 아예 등록돼 있지 않다.** `last_error_date` /
`last_error_message` 필드가 **아예 없는** 것이 결정적이다: 텔레그램은 배달을 시도한 적이
없으므로 실패 기록도 없다. ⇒ **Cloudflare 차단이 아니다.**

한때는 동작했다 — `trigger_events` 의 유일한 telegram 행(2026-08-09)은 `trigger_kind`
가 `webhook` 인 진짜 수신(`"hi"`)이다. 그 뒤 어느 시점에 풀렸다.

**진짜 결함: 제품이 웹훅을 등록하지 않는다.** `setWebhook` 호출이 코드베이스 전체에
**0개**다(`plugin/telegram/client.py` 에는 `send_message` · `answer_callback_query` ·
`edit_message_text` · `delete_message` 만 있다). 그래서 **한 번 풀리면 아무도 되돌리지
않는다.**

**증상이 비대칭이고 조용하다.** 아웃바운드(박스 → 텔레그램)는 멀쩡하므로 알림 자체는
계속 나간다. 그 중 **`shipped` 이벤트에만** 승인/거절 버튼이 붙는데(아래 정정 참조),
그 버튼을 누르면 텔레그램이 `callback_query` 를 배달할 곳이 없어 **아무 일도 일어나지
않는다.** 커넥터 행의 `webhook_trigger: true` · `interactive_approval: true` 는 제품이
지킬 수 없는 주장이었다.

현재 대기 중인 승인은 없다(`safe_mode` · `checkpoints` · `decisions` 세 큐 전부 0) —
**영향은 잠복 상태**다.

**수리 순서가 강제된다**: 텔레그램 서버는 데이터센터 IP 다. 지금 `setWebhook` 을 배선해도
위의 CF challenge 에 걸릴 가능성이 높다. ⇒ **① CF 해제(형님) → ② `setWebhook` 배선(코드)**
순서여야 하고, ②만 먼저 하면 등록은 되지만 배달은 계속 실패한다.

### 상태

- PR #918 은 머지됐고 **스케줄은 `disabled_manually`** 로 꺼 뒀다 — CF 가 풀리기 전에는
  15분마다 실패 메일만 보내기 때문이다. `gh workflow enable offbox-uptime.yml` 한 줄로 살아난다.
- 수리는 **Cloudflare 콘솔**(리포 밖): `api.bsvibe.dev` 에 대한 Bot Fight Mode / WAF
  managed challenge 를 조정해야 한다. 보안 설정이라 임의로 만지지 않았다.
- **이 검사는 CF 가 풀렸는지 판정하는 도구이기도 하다** — enable 후 초록이면 풀린 것이다.

### 규율

**주거용 IP 에서만 테스트하면 다중 사용자 경로를 영원히 못 본다.** 이 결함은 코드에
없고 어떤 테스트로도 안 잡히며, 형님 머신에서 `curl` 하면 200 이다. 박스 밖 vantage
point 를 하나 갖는 것 자체가 진단 능력이었다.

### ✅ 원인 확정 + 해소 (2026-09-11)

형님이 **Bot Fight Mode 를 껐고**, 데이터센터 IP 재측정에서 **전 표면이 뚫렸다**.
`cf-mitigated` 헤더가 모든 응답에서 사라졌다.

| 표면 | 전 | 후 |
|---|---|---|
| `/install-worker.sh` | 403 | **200** |
| `/api/v1/workers/register` | 403 | **422** (빈 body 검증오류 = 앱이 처리) |
| `/api/v1/workers/poll` · `/heartbeat` | 403 | **401** (가짜 토큰 = 앱이 처리) |
| `/mcp` | 403 | **307** |
| `/api/oauth/device_authorization` | 403 | **200** |
| `/api/webhooks/{telegram,github}/…` | 403 | **404** (가짜 토큰 = 앱 도달) |
| `/api/auth/login` | 403 | **401** (Supabase 정상) |
| `/api/health` · `/redoc` | 403 | **200** |

**⇒ 게이트 2 의 다중 사용자 차단이 실제로 풀렸다.** 클라우드 VM 워커 등록·폴링과
`curl | sh` 온보딩이 이제 가능하다.

박스 밖 감시 워크플로는 **`active`** 로 복구됐고 main 판본이 초록이다.

⚠️ **네비게이션 경로 정정**: 이 문서 초판이 적었던 "Security → Bots" 와
"Security → Events" 는 현행이 아니다. Cloudflare 공식 문서(2026-08 갱신) 기준:
Bot Fight Mode 는 **Security → Settings → "Bot traffic" 필터**,
보안 이벤트 로그는 **Analytics → Events 탭**이다. 측정 없이 기억으로 쓴 경로였다.

### 다음 (순서가 강제됨)

CF 가 풀렸으므로 텔레그램 `setWebhook` 배선(위 §텔레그램 인바운드)의 선행조건이
충족됐다. 이제 코드로 수리할 수 있다.

### ✅ 텔레그램 인바운드 — 수리·배포·실증 완료 (2026-09-11, PR #920)

제품이 웹훅을 등록하게 했다. prod `0cf8b13`.

**⚠️ `setWebhook` 만 불렀으면 아무것도 안 고쳐졌다.** 리졸버는 인바운드를
`delivery_config["webhook_secret"]` 로 검증하고 없으면 서명 시크릿(= **봇 토큰**)으로
폴백하는데, 봇 토큰엔 `:` 가 있고 텔레그램의 `secret_token` 은 `A-Za-z0-9_-` 만 받는다.
prod 커넥터는 `{"chat_id": …}` 뿐이었으므로 **등록은 되고 모든 업데이트가 검증 실패**하는
상태가 됐을 것이다. 그래서 등록이 시크릿을 만들어 저장하고 같은 값을 텔레그램에 넘긴다.

**전후 대조 (prod 실측)**

| | 전 | 후 |
|---|---|---|
| `getWebhookInfo.url_set` | **False** | **True** |
| `delivery_config` 키 | `['chat_id']` | `['chat_id', 'webhook_secret']` (len 43, `:` 없음) |
| 등록 URL 이 우리 ingress 인가 | — | **True** |

**공개 URL 로 실제 업데이트를 흘린 결과 (음성 대조군 포함)**

| 요청 | 결과 |
|---|---|
| 등록된 시크릿 + `edited_message`(파서가 스킵) | **202** — 검증 통과 |
| **틀린 시크릿** | **401** — 위조 거절 |
| **시크릿 헤더 없음** | **401** |
| 등록된 시크릿 + `message` | **202**, `trigger_events` **1 → 2** |

⇒ 체인이 끝까지 통한다: CF → ingress → 리졸버 검증 → 파서 → intake.
시크릿이 **실제로 무는 것**까지 확인됐다(401 두 건).

검증용 프로브가 만든 런 `8bfd51e0` 은 폐기 처리했다.

**남은 것**: 형님이 텔레그램에서 실제 승인 버튼을 눌러 보면 `callback_query` 경로까지
끝난다. 위 실증은 `message` 업데이트로 intake 도달을 증명했고, `callback_query` 는
같은 ingress·같은 시크릿 검증을 지난 뒤 `telegram_callback` 어댑터로 간다.

### ⚠️ 정정 (2026-09-11) — "승인 버튼을 계속 받는다"는 틀렸다

형님 지적: *"텔레그램에서는 요약으로 가는 링크만 있고 버튼이 없어."* 맞다.
위 문단들이 원래 "형님은 승인/거절 카드를 계속 받는다"고 적었는데 **확인 없이 쓴 서술**이다.
PR #920 본문·커밋 메시지·모듈 독스트링에도 같은 문장이 들어갔다.

**배포된 코드를 실제 행으로 렌더해 측정한 결과:**

| 이벤트 | 버튼 | 링크 |
|---|---|---|
| `shipped` | **Approve / Reject** | `/deliverables/…` |
| `needs_you` | **없음** | `/brief` |
| `daily_brief` · `triggered` · `auth_down` | 없음 | `/brief` |
| `failed` | 없음 | `/runs/…` |

버튼은 `_approval_keyboard`(`notify_builders.py`)가 **`event == "shipped"` 이고
`deliverable_id` 가 있을 때만** 만든다. 체인 자체는 온전하다 — 빌더가 `reply_markup` 을
붙이고 `plugin/telegram/plugin.py:deliver_message` 가 그대로 넘긴다(실측 확인).

**PR #920 의 전제는 여전히 참이다**: 웹훅은 실제로 등록돼 있지 않았고(`url_set: False`),
`shipped` 의 Approve/Reject 콜백은 갈 곳이 없었다. 틀린 건 "매번 버튼을 받고 있었다"는
빈도 서술뿐이다.

### 🔶 새로 드러난 제품 갭 — 답이 필요한 알림에 인라인 액션이 없다

`needs_you` 는 Decision 이 답을 기다리는 상태인데 텔레그램에서는 **링크를 타고 브리프로
가야만** 답할 수 있다. CTA 문구가 코드 주석에 그대로 `"요약에서 답해주세요 → …/brief"` 다.
배달 승인(`shipped`)에만 버튼이 있고, **가장 인터랙션이 필요한 이벤트가 링크뿐**이다.

⚠️ 임의로 고칠 것이 아니다 — 제품 결정이 필요하다:
`needs_you` 의 Decision 은 선택지가 2개 고정이 아니라 **가변 `options` 배열**이므로,
인라인 키보드로 옮기려면 ① 어떤 Decision 종류까지 버튼화할지 ② 옵션이 많거나 자유
텍스트가 필요할 때 어떻게 할지 ③ `callback_data` 64바이트 제한을 어떻게 다룰지를 정해야
한다. 텔레그램 커넥터가 `interactive_approval: true` 인 만큼 갭은 실재한다.

---

## ✅ 해소됨 (2026-09-11) — 제품 × 커넥터 바인딩 [세 경로 전부]

형님 질문에서 드러났다: *"텔레그램 봇으로 BStockReport 에 작업 지시 가능한 상태야?"*
→ 아니오. 그리고 파고드니 **감사 본문이 전혀 다루지 않은 축**이 나왔다.

형님 판정: *"당연히 제품 별로 봇, 이슈, 채널 등을 다 분리할 수 있어야 bsvibe 제품의
철학에 맞아."*

### 설계는 이미 있다 — 그리고 형님 데이터도 이미 맞다

`resource_bindings` 독스트링 첫 줄이 **"Per-Product × Connector 3-knob binding
(Workflow §3)"** 이고, 인바운드 해석까지 예고해 뒀다:

> *"the `(connector_account_id, resource_id)` index is what Receive (B10b) will use to
> resolve an inbound webhook → binding → **Product**"*

prod 실측 — 형님이 이미 올바르게 채워두셨다:

| 제품 | 커넥터 | `resource_id` | trigger |
|---|---|---|---|
| **BStockReport** | telegram | **`8242700007`** (그 chat_id) | `enabled: False` |
| BSVibe | github | `BSVibe/bsvibe-app` | `enabled: False` |

### 그런데 라우팅 소비자가 없다 (컬럼별 실측)

| 컬럼 | 읽는 곳 | 실제 |
|---|---|---|
| `product_id` | 4 | 전부 **CRUD/MCP 툴**. 라우팅 로직 **0** |
| **`resource_id`** | 1 | 리포지터리 조회 헬퍼 하나 — **호출자 0** (B10b 미배선) |
| **`trigger.enabled`** | **0** | 모델 독스트링·API 스키마·마이그레이션뿐. **소비자 없음** |
| `output_mode` | 3 | ✅ 유일하게 실제로 쓰임 |

⇒ `trigger` 는 *"do I act"* 노브로 문서화돼 있는데 **아무것도 안 읽는다** —
스킬 `config-menu-offers-options-nothing-implements` 모양. UI 에서 켜고 꺼도
아무 일도 안 일어난다.

### 세 경로가 전부 그 축을 비껴간다

| 경로 | 바인딩 사용 | 결과 |
|---|---|---|
| **알림** | ❌ `workspace_id` + `is_active` 만(`bindings.py:65`) | **전 제품 알림이 전 채널로**. 본문에 제품명도 없음 |
| **인바운드** | ❌ `external_ref` → repo 만 | 채팅 커넥터는 **제품 없음** |
| **배송** | 🔶 `connector_account_id` 만 뽑아 **불리언 게이트**로 | *"이 계정이 배송 대상인가"*. `product_id`·`resource_id` 안 읽음 |

⚠️ **정정**: 조사 초반에 "배송은 제품별로 분리돼 있다"고 적었는데 **틀렸다.**
`_resolver.py` 는 워크스페이스 전체에서 `connector_account_id` 집합만 만든다.
배송의 *"guard against IMPLICIT ROUTING"* 은 **제품 라우팅이 아니라 계정 허용 목록**이다.

### 채팅 커넥터 전체가 같은 증상 — 구조적이다

형님 추정 *"디스코드, 슬랙 같은거도 같은 증상일듯"* → **맞고, 원인이 하나다.**

**어떤 플러그인도 `product_id` 를 정하지 않는다** (실측: telegram·discord·slack·
github·sentry 파서에서 `product` 언급 **0줄**, `plugin/` 전체에서 `product_id`
세팅 **0건**).

제품 해석기는 라우트의 `_product_id_for_repo` **단 하나**이고, 그 입력은
`payload["repo"]` 또는 `account.external_ref` 다. 그래서:

* **github** 만 동작한다 — 파서가 `payload["repo"]` 를 넣기 때문(`webhook.py:130`)
* **telegram / discord / slack** 은 repo 개념이 **0줄** → **구조적으로 제품 없음**

⇒ *"repo 모양 폴백이 유일한 제품 해석기"* 라서, **repo 를 갖지 않는 커넥터는
설계상 전부 제품 미해결**이다. 채팅 커넥터를 하나 더 붙여도 같은 자리에 떨어진다.

### 왜 지금까지 안 보였나

제품이 **하나**일 때는 워크스페이스 = 제품이라 아무 증상이 없다. **둘이 된 순간**
어긋나기 시작했고, 다중 사용자 개방이면 더 나빠진다. 감사 본문이 이 축을 못 본 이유도
같다 — 단일 제품 관점에서는 모든 경로가 "맞게" 동작한다.

### 범위 (착수 전 형님과)

세 경로를 다 건드리고, 오늘 낸 8개 PR 어느 것보다 크다. 순서만 제안:

1. **인바운드 제품 해석** — `(connector_account_id, resource_id)` → binding → product.
   헬퍼와 인덱스가 이미 있다(B10b). 채팅 커넥터 전부가 이걸로 해결된다
2. **알림 라우팅** — 제품별 채널. ⚠️ 다만 *"알림은 사람에게, 배송은 제품에게"* 가
   의도일 수 있으니 먼저 확인 (오늘만 두 번, 감사가 지목한 "구멍"이 근거까지 적힌
   의도된 설계였다 — `run_caps` · `verify_slots`)
3. **`trigger.enabled` 를 배선하거나 지운다** — 소비자 0인 노브는 틀린 기본값으로
   작동한다. 형님 원칙: 안 쓰이는 설정은 지워라
4. **배송을 불리언에서 라우터로** — 1·2 가 정해진 뒤에. 지금은 계정 허용 목록이라
   제품이 늘어도 "누구에게 보낼지"를 못 고른다

### ✅ 1번(인바운드 제품 해석) 해소 — PR #922, prod `69556d9`

`resource_bindings` 의 `(connector_account_id, resource_id)` 를 라우트가 **repo 폴백보다
먼저** 읽는다. 커넥터별 리소스 키를 **선언**한다(telegram `chat_id` · discord
`channel_id` · slack `channel` · github `repo` · sentry `project`) — 추론하지 않는다.
텔레그램은 `chat_id` 를 JSON 숫자로 보내므로 문자열 캐스팅이 필수다.

**prod 실측 (전후 대조)**

| 런 | 제품 | 시각 |
|---|---|---|
| `08547545` | **BStockReport** | 07:31Z (PR #922 이후) |
| `314ca8d9` | `None` | 06:31Z (이전) |

⇒ 형님이 넣어두셨던 `BStockReport × telegram × 8242700007` 이 **드디어 읽힌다.**
`trigger_events` 의 `product_id` 도 처음으로 채워졌다(2→3번째 행).

**가드가 실제로 일했다**: *"웹훅 가능한 커넥터는 전부 리소스 키를 선언해야 한다"* 는
테스트가 **sentry 누락을 잡았다** — telegram 만 고쳤으면 discord·slack·sentry 가 같은
바닥에 남았을 것이다.

⚠️ **바인딩은 1:1 채팅(`8242700007`)을 가리킨다.** 그룹방(`-1003257931284`)에서 보낸
메시지는 안 붙는다 — 설정이지 결함이 아니다. 쓸 방을 정해 바인딩을 맞춰야 한다.

⚠️ **BStockReport 는 `client_attach`** 다. 런이 제품에 붙는 것까지가 이 PR 의 범위이고,
그 실행 모드가 채팅 지시에서 실제로 도는지는 **아직 안 쟀다**.

**남은 축은 그대로다**: 알림(워크스페이스 단위) · `trigger.enabled`(소비자 0) ·
배송(불리언 게이트).

### ✅ 세 경로 전부 해소 (2026-09-11)

| 경로 | PR | 무엇이 바뀌었나 |
|---|---|---|
| **인바운드** | #922 | `(connector_account_id, resource_id)` → binding → product. 커넥터별 리소스 키를 **선언**(telegram `chat_id` · discord `channel_id` · slack `channel` · github `repo` · sentry `project`). prod 실측: 텔레그램 런의 `product_id` 가 `null` → **BStockReport** |
| **알림** | #923 | `emit_notification` 이 `product_id` 를 싣고, 카드 제목이 `[BStockReport] 작업 완료`. 채널은 제품 바인딩으로 좁히되 **폴백** |
| **배송** | #925 | `_resolve_bindings` 가 제품 범위. **폴백 없음** |
| `trigger.enabled` | #924 | 소비자 0인 노브 **삭제**. `filters` 는 유지 |

**⚠️ 폴백 정책이 알림과 배송에서 반대다** — 알림은 **잃는 게** 더 나쁘고(바인딩 없으면
워크스페이스 전 채널), 배송은 **엉뚱한 데 쓰는 게** 더 나쁘다(바인딩 없으면 안 보냄).
github 독스트링이 그 원칙을 이미 적어 뒀다: *"None 은 의도된 안전한 결과이며, 제품이
소유하지 않은 repo 에 쓰는 것보다 낫다."*

### ⚠️ 조사 중 제 진단 셋이 틀렸다 — 기록해 둔다

1. **"B10b 미배선"** → 틀림. Receive 스테이지(`stages/intake.py`)는 **존재하고
   IntakeWorker 가 실제로 부른다**. 정확히는 *배선돼 있지만 입력(페이로드의
   `connector_account_id`/`resource_id`)이 안 들어온다* — 그 코드 주석이 상태를 이름까지
   붙여 뒀다: *"an inbound parser that hasn't been retrofitted yet"*. ⇒ #922 는 중복은
   아니지만 **설계가 의도한 자리가 아니다**(라우트에서 직접 조회). `receive()` 의 폴백이
   `trigger.product_id` 를 실어 나르므로 충돌하진 않는다. 언젠가 통합 여지.
2. **"배송은 불리언 게이트"** → 절반만 맞음. **github 은 이미 제품 라우터**였다
   (#681·#684·#723). 비-github 경로만 워크스페이스 전역이었다.
3. **"배송은 제품별로 분리돼 있다"**(조사 초반) → 틀림. `_resolve_bindings` 는
   `connector_account_id` 집합만 뽑는다. 2번에서 자체 정정.

### 남은 것

* **배송이 아직 발화한 적 없다** — 산출물 251건에 `delivery_events` **0**(바인딩 둘 다
  `output_mode: safe`). #925 는 잠복 결함을 닫은 것이고, prod 동작 변화는 없다.
* **BSVibe 에 배송 바인딩이 없다** — #925 이후 BSVibe 산출물은 커넥터 배송이 안 나간다
  (github 은 별도 경로라 영향 없음). 필요하면 바인딩을 추가해야 한다.
* **prod 바인딩 JSON 에 `"enabled": false` 잔해** — 아무도 안 읽으니 무해하지만 잔해다.
* **`client_attach` 미확인** — BStockReport 가 `client_attach` 인데, 텔레그램 지시가 그
  실행 모드에서 실제로 도는지 아직 안 쟀다.
