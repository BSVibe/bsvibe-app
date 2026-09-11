# BSVibe — Master Status

"BSVibe 전체가 어디까지 와 있나" 의 단일 진실의 원천 (SoT). Living document —
이 파일을 in-place 갱신, 새 status 파일 만들지 말 것.
**최근 리뷰: 2026-09-10.** 범위: 통합 단일제품 `bsvibe-app`.
**현 단계: 출시 게이트 클리어(GO) 이후 — 실행 모델 재설계 → 검증 사정거리 확장 → **척추(trust ratchet) 복원**.**
**Prod = main `9983710`** (2026-09-10, #905~#913 배포 완료 — 다중사용자 준비도 감사 + 게이트0~3. 배포본 실행검증).

> ⚠️ **이 파일은 2026-08-18 ~ 08-26 사이 두 군데가 틀려 있었다** (2026-08-26 실측으로 정정):
> R2 cutover 를 *"아직 local"* 이라 적었으나 prod 는 **`BSVIBE_PRODUCT_BUNDLE_BACKEND=s3`**
> (컷오버 완료), launchd worker 를 **2대**라 적었으나 실제 **3대**다.
>
> 🔴 **이 파일과 `BSVibe_Roadmap.md`(2026-06-24) · `BSVibe_Reality_Audit_2026-07-14.md`
> 는 실제 트랙보다 뒤처져 있다.** 최근 두 달의 실작업(신뢰 래칫 · 판정 축 재정의 ·
> 구조 삭제 · 검증 게이트 정합성)은 **세션 인수인계 사슬**에만 있다.
> **→ 현행 SoT: `internal-docs:BSVibe_Handoff_Prompt_2026-09-09-b.md` +
> `docs/design/pipeline-removal-routing.md` §10~§24.**
>
> ⚠️ **백로그 문서의 항목을 근거로 작업을 시작하지 마라 — 먼저 prod/코드로 다시 재라.**
> 2026-08-26 스팟체크: Reality Audit 의 `3.5-1`(knowledge_search 항상 에러) 과
> `E2`(file_edit 스키마 불일치)는 **둘 다 이미 해소**됐다(코드에 수정 주석이 남아 있다).
> 감사 항목은 **재측정 전에는 열린 것으로 취급하지 마라.**

> 🔴 **2026-08-18 — 판정 축이 계획에서 벗어나 있음이 실측으로 확인됐다.**
> 형님 원안은 *"AI가 작업 전 검증 방법을 선언 → 실행 → 전부 성공하면 통과"* 인데,
> 실제로는 **유사도 검색된 지식이 자동으로 LLM 판사의 합격 기준**이 되고 있다
> (`rationale: "Canonical patterns retrieved for this change"`). 8월 실측: 에이전트
> **선언 검사 실패 0건**, LLM 판사 단독 거절 66건 = **런 5개**(그중 2건은 판사 자기 실명),
> 런당 13.2회 루프. → `internal-docs:BSVibe_Verdict_Binarization_Design.md` **§8** (§3 은 폐기)

> ### 🆕 2026-08-26 → 09-03 사이에 바뀐 것 (#827~#879)
>
> 이 구간의 상세는 전부 **세션 인수인계 사슬**에 있다(현행: `BSVibe_Handoff_Prompt_2026-09-02-b.md`).
> 여기엔 **이 파일의 다른 절을 무효화하는 것만** 적는다.
>
> * **운영 특성 — `var/runs` 는 스스로 안 묶인다. 사람의 리뷰 큐에 묶인다.**
>   리퍼는 정상이다 — `_TERMINAL = {shipped, failed, cancelled}` 이고 `review_ready`
>   는 "리뷰 중 = 워크스페이스 사용 중"이라 정당하게 안 지운다.
>
>   | 실측 | `/app/var/runs` | 디렉터리 | `review_ready` | `cancelled` | `failed` | `shipped` |
>   |---|---|---|---|---|---|---|
>   | 2026-09-03 | 25 GB | 103 | 103 | 68 | 55 | 12 |
>   | **2026-09-09** | **1.3 G** | **30** | **30** | **156** | **55** | **22** |
>
>   ⇒ **기제가 실증됐다.** 리퍼 변경 없이 디스크가 25GB → 1.3G 로 줄었다 — 형님이
>   리뷰 큐를 소화한 결과다(`shipped` +10, `cancelled` +88). 디스크는 코드가 아니라
>   **큐 길이의 함수**다.
> * **✅ 무료 플랜 동시 런 상한 — 구현됐다 (#881, `20260903_workspace_run_cap`).**
>   ⚠️ 이 항목은 09-03 에 *"미구현 · 플랜/티어/쿼터 컬럼 0개"* 로 적혀 있었다. 실측
>   (2026-09-09): `workspaces.max_concurrent_runs` 존재, `server_default = 3`.
>   prod 3행 중 **2행 = 3, 1행 = NULL**(형님 워크스페이스 — 마이그레이션이 UPDATE 로 뺐다).
>   `NULL` = **uncapped**, 없는 워크스페이스는 **기본값으로** 떨어진다(`run_caps.py` —
>   *"모르는 워크스페이스가 게이트를 통과하는 가장 싼 길이면 안 된다"*).
>   ⭐ **기본값이 DDL-side 라 INSERT 로는 "uncapped" 를 말할 수 없다** — 새 워크스페이스는
>   uncapped 로 태어날 수 없고, 플랜에서 빼는 것은 **UPDATE** 다. 가격 레버로는 이 방향이 맞다.
>   `sandbox_max_concurrent=2` 는 여전히 **다른 축**(도커 세마포어)이라 재사용 금지.
> * **온보딩 라이브 E2E 를 처음으로 걸었다 (#879)** — 그리고 체크리스트의 두 항목이
>   *프로덕션이 만들 수 없는 상태*를 적고 있었다(둘 다 결함, 수정됨). 유닛은 전부
>   초록이었다. 새 스위트 `apps/pwa/e2e-live/onboarding.spec.ts`.
> * **PWA 배포 경로 주의** — `/api/health` 의 `git_sha` 는 **백엔드 것**이다.
>   autodeploy 는 `pwa` 를 의도적으로 제외하고(Vercel 프론트), PR 의 Vercel 체크가
>   `Canceled by Ignored Build Step` 인 것은 정상이다(preview off). PWA 가 실제로
>   나갔는지는 **GitHub deployments + 청크 해시 대조**로 확인하라.

> ### 🆕 2026-09-03 → 09-09 에 바뀐 것 (#880~#906, 26 커밋)
>
> 상세는 세션 인수인계 사슬에 있다(현행: `BSVibe_Handoff_Prompt_2026-09-09-b.md`,
> 그 앞은 `2026-09-09.md` · `2026-09-08.md` · `2026-09-07.md`).
> 여기엔 **이 파일의 다른 절을 무효화하거나 새로 확정된 것만** 적는다.
>
> * **🪦 producer 없는 `audit_events` 테이블이 GDPR Art.30 보존 약속의 주어였다**
>   (#905·#906). 0행 테이블에 *"Retained 1 year for security incident review"* 를
>   약속하고 있었고, **실제 기제는 `audit_retention_days`(NULL = forever 가 기본,
>   prod 3/3 NULL)** 라 정리한다고 말하면서 영구 보관하고 있었다. 문구 정정 + 테이블
>   DROP 완료. alembic head = `drop_producerless_audit_ev`.
>   ⇒ **죽은 것은 무해하지 않다.** 삭제 후보를 만나면 "쓰는 곳"과 **"말하는 곳"**을
>   따로 세라 — 감사는 전자만 세고 사용자에게 나가는 건 후자다.
> * **⚖️ 거절의 두 행위가 갈렸다** (#902, `DenyKind`). `deny` 동사 하나가 *접근 거절*과
>   *큐 정리*를 같이 처리해서, 형님이 큐를 치우며 적은 *"내용 문제 아님"* 이
>   **LLM 판사의 합격 기준**이 되고 있었다(vault 오염 25건 중 17건 회수).
> * **🧹 결정 경로의 폐기도 Safe Mode 대기 항목을 정리한다** (#903) — `cancel_run` 이
>   이미 하던 것을 `checkpoints_resolve(discard)` 는 안 했다(orphaned-half).
> * **🩺 raise 하는 파일 연산이 awaiter 의 관측을 실어 나른다** (#904) —
>   `test_list_dir_returns_entries` 가 다섯 달째 **무관한 PR** 의 CI 를 터뜨리는데 매번
>   손에 쥔 게 `exit None` 한 줄이었다. **원인은 여전히 미상** — 다음 빨강에서 숫자를 읽어라.
> * **🚦 CI 실패 판정이 되돌릴 수 있게 됐다** (#898) — 같은 head 에서 두 번 봐야 부른다
>   (`ci_red_head_sha`). **아직 한 번도 발화 안 함**(0/36).
> * **🔁 프레이밍 실패에 유계 재시도 + `run_drive_failed` Decision** (#897) —
>   **아직 한 번도 발화 안 함**(0건). 트리거가 일시적 LLM 실패라 **프로브로 못 켠다**.
>
> ⚠️ **위 둘(#897·#898)은 자연 발생 대기다.** 베이스라인과 발화 신호는
> `BSVibe_Handoff_Prompt_2026-09-09-b.md` §Ⅲ 에 있다 — 켜보려고 런을 발주하거나
> 고의로 CI 를 깨뜨리지 마라.

2026-07-04 이후의 큰 변화는 **"BSVibe가 어디서 실행하고, 무엇을 증명하는가"** 두 축이다.

- **실행 위치**: server_sandbox 단일 모델 → **client_attach 추가**(#693~#701). 에이전트가 파운더의
  기존 작업 디렉터리에서 CLI 네이티브 툴로 실행. 서버는 소스를 clone·저장·push 하지 않는다(§3.5 프라이버시 계약).
- **증명**: client_attach run이 파운더 머신에서 **레포 자신의 게이트를 돌려** 정직한 `PROVED`에 도달
  (#702~#705, #716~#718). 라이브 실증 완료.
- **모드 파리티**(#719~#721): 실행 위치 선택이 플랫폼 능력(지식·질문·딜리버러블 발행)까지 꺼버리던 결함 해소.
  툴 표면을 **워크스페이스 축 / 플랫폼 축**으로 분리, 대조표를 테스트로 고정.
- **BStockReport M5 자동 완주**(2026-08-10): 스케줄 자동 발사 → 파운더 머신 실행 → 딜리버러블 →
  승인 → 텔레그램. 사람 손 없이 전 구간 통과.

⚠️ **알려진 한계**: ~~검증은 여전히 "레포가 자기 검사를 통과했다"까지다.~~
**2026-08-18 실측으로 부분 해소** — `outcome_demonstration` 프로브가 프로덕션에서 돌고 있다
(주당 매치 수: 07-27 37 · 08-03 102 · 08-10 187). 검증은 이제 "레포 자기 검사" + "결과 시연"이다.
남은 한계는 *배포된 프로덕션*을 찌르는 축(§ production_probes 설계). 2026-08-09~10 이틀간
프로덕션 결함 6건을 잡았는데 **전부 유닛 green**이었고 전부 손으로 돌린 E2E에서만 드러났다. 설계 =
`docs/design/production-verification.md` (proof_state 환경 축 · production_probes · 배포 감지).

✅ **R2 번들 cutover 완료** (2026-08-18 실측): 프로덕션은 `BSVIBE_PRODUCT_BUNDLE_BACKEND=s3`,
엔드포인트는 Cloudflare R2. 제품 번들이 앱 디스크 밖에 산다 = durable.
절차 기록 = `BSVibe_Product_Bundle_R2_Cutover_Runbook.md`.
> ⚠️ 이 항목은 2026-08-18 까지 *"미완, backend=local, 아직 durability 아님"* 으로 적혀 있었다.
> **문서가 이미 해소된 위험을 경고하고 있었다** — 디스크 풀이 복구불능 브릭인 제품에서 특히 나쁜 종류의 어긋남.

### 척추 복원 — trust ratchet (2026-08-15~18, 최신 트랙)

형님 지적 *"검증 시스템이 초기 계획에서 많이 벗어난듯한데, 애초에 abcd 등급도 내 계획엔 없었어"* 에서 출발.
드리프트 감사(`BSVibe_Plan_Drift_Audit_2026-08-15.md`) 9건 중 **7건 해소**.

- **사슬이 이어졌다** (#758~#763): 형님 거절 → 저장 → 지식화 → 검색 → **선언 시점에 에이전트에게**.
  2개월간 91번의 "아니오"가 `del reason` 으로 사라지고 있었다. 오늘 이후로는 남는다.
  SoT = `BSVibe_Trust_Ratchet_Restoration_Design.md`.
- **판사가 못 본 채 판정하지 않는다** (#764): `git diff` 기반 + 잘림 시 `cannot_determine` **fail-open**.
- **인프라 원칙** (#751~#753): 실패 피드백이 실패부터 말한다 · executor 턴이 흔적을 남긴다 ·
  플랫폼이 자기 능력을 말한다. (형님: *"우리가 만드는 건 인프라, 볼 수 없는 이슈는 치명적"*)
- **라우팅 glass-box** (#765): 어느 모델이 왜 골렸는지 타임라인에 남는다. 실 런 확인 완료.
- **레지스트리가 유령 caller 를 팔던 문제** (#766): 형님 룰 하나가 **두 달간 inert** 였다.
  근본 원인은 레지스트리가 *"설정 가능"* 과 *"실제 발화"* 를 뒤섞은 것. AST 구조 게이트 신설.
- **알림 정확도** (#754) · **런타임 분해** (#767) · **타임라인 한국어화** (#768).
- **승인 큐 누수** (#769): `delivery_worker` 가 *"런의 partial 을 ONE transaction 으로"* 라고
  코드에 계약을 적어뒀는데 **폰·REST·MCP 세 표면 전부 항목 단위**였다. 멀티아티팩트 런의
  N-1 행은 어떤 알림도 지목하지 않아 **도달 불가능**했다(런 하나가 18행 중 17행 잔류,
  대기 46건 중 35건이 그 모양). **형님이 밀린 게 아니라 큐가 샜다.** 런 단위 거절도 신설.
- **대기 전량 청소**: Safe Mode 46 → **0**, Decision 3 → **0**. 청소성이라 사유를 비웠고,
  `deny_reason` 46건 전부 **NULL**·`negative_pattern` 2건 그대로 = **지식 오염 0** 실측.

⚠️ **다음**: ①A-1c 감쇠 지표는 **사유 있는 거절이 2건뿐**이라 여전히 이르다(청소성 46건은 잴
대상이 아니다) ②감사 D(`surface_exercised`·`scope.verdict` 읽는 곳 0) ③`worker_runtime.py` 가
**정확히 400 LOC** — 다음 분해 후보.

⚖️ **형님 판정 기록**: 라우팅 룰이 워크스페이스별로 다른 것을 결함으로 올렸다가 바로잡혔다 —
*"룰은 강제가 아닌 사용자 커스텀이야. 굳이 우리가 보정할 이유는 없어."* 조치하지 않는다.

<details><summary>이전 단계 요약 (2026-07-01~04, 출시 게이트)</summary>

verify 무결성 재설계(#481~490) 후 전수 E2E서 verified false-positive **0/35** 실증 + founder 코어 UX 2건
+ polish 3건(#492). 2026-07-01 NO-GO 코어 blocker(verify가 garbage 통과) 해소. verify LLM repo-grounded
gate 재설계 #494 — 스택-하드코딩 폐기, repo 매니페스트서 LLM이 검증명령 도출→결정론 실행.
git_ops PAT 잔존 결함 #496 수정 + prod 라이브 검증. 커넥터 UX 3연쇄(#497/#499/#500). **출시 게이트 전부 클리어 = GO.**
</details>

> **시대 구분.** 이전 4제품 + auth-server + site 구도
> (BSGateway / BSage / BSNexus / BSupervisor 각각 standalone 배포) 는
> 2026-05 피벗으로 **대체됨**. 그 역사는
> `archive/BSVibe_Status_4product_era_2026-05-18.md`. 이후 2026-05-26 ~ 2026-05-30
> 의 audit 복구 시리즈 (B1–B17) + Phase 1 routing + D1–D6 near-term refinement
> 까지가 "기능 갭 클로즈" 단계였고, 그 산출물을 받은 **v8 클래스 아키텍처
> migration (2026-05-30 → 2026-06-02, 41 PR + 3 hot-fix)** 가 코드 구조 자체를
> facade + bounded-context 모델로 재정렬했다. 이 문서는 v8 migration **이후**
> 상태를 반영한다. v8 migration **이전**의 진행도 표는 archive 의 이전 Status
> 스냅샷 + git history 에서 추적.

---

## 1. BSVibe 란

**한 줄:** 한 사람이 여러 제품을 동시에 운영하기 위해 쓰는 운영체제.
실제 OS 가 여러 프로그램을 — glanceable, scheduled, isolated, 필요할 때만 개입 —
돌리듯, BSVibe 는 여러 AI 작업 스트림을 돌린다.

**해소하는 통증:** Claude Code 베이비시팅 (지켜보고, 확인하고, 지시하기) 은
지루하고, *특히* 동시에 여러 개를 돌릴 때 그렇다. 파운더는 제품을 찍어내고 싶고,
그건 단일 앱이 아니라 OS 가 필요하다.

**Wedge (외부 피치):** *검증된 작업 (verified work) + 같은 실수를 반복하지 않는
에이전트.* 출시 경쟁자 전체 (Notion 3.5, Paperclip, Codex, Devin, Cursor) 에
공통으로 비어 있는 두 칸: **결과별 파운더가 읽을 수 있는 증거 표면 (per-result
proof surface)** 과 **작업 간 학습 루프 (cross-task learning loop)**. 그 간극이
moat. "AI agent OS" 라는 표현은 **내부** 아키텍처 프레임으로만 유지한다
(외부 카테고리는 Paperclip 이 점유).

**비즈니스 모델:** BSVibe (OS) 가 판매되는 SaaS. 그걸로 찍어내는 제품들은 별개
사안. 타깃 시그널: 여러 제품을 위임하며 운영하는 1인 파운더.

**전략 SoT:** `docs/architecture/strategy-synthesis.md`
(+ `docs/architecture/ux-design.md`, `docs/architecture/workflow-backend.md`).

**제품 형태:** 단일 `bsvibe-app` (Next.js PWA + FastAPI monolith). PWA
`app.bsvibe.dev` (Vercel, main 머지 시 auto-deploy); API `api.bsvibe.dev`
(Cloudflare tunnel → Mac Mini backend `localhost:8700`). `bsvibe-site`
(`bsvibe.dev`) 는 CTAs-only 마케팅 사이트 (별도 repo, live).

---

## 2. 아키텍처 — v8 facade + 6 bounded context

**4 줌 레벨** (네비게이션은 줌, "4제품" 아님): **L0 Fleet** (모든 제품 한눈) →
**L1 Product** → **L2 Run** (위임 단위 1개 + 증거) → **L3 Process**
(라운드별 내부).

**Founder shell — 살아남는 4개** (maximally simple, 5번째 없음):
**Direct** (입력, 어느 레벨이든) · **Brief** (L0 Fleet, 홈) · **Decisions**
(개입 inbox) · **Inside** (Run/Process 증거 + 온톨로지 drill-in).

**워크플로 (lock):** `Receive → Frame → Agent loop ↻ → ε`, 그리고 **Deliver** 와
**Settle** 은 루프가 진행 내내 emit 하는 연속 side channel.

**척추 — 신뢰 래칫 (trust ratchet)** (*속성*, 엔티티 아님): 신뢰를 누적시키는
2 개의 일방향. (1) 작업 루프는 일방향 (plan→act→verify→next; verify 실패는 새
plan 을 트리거할 뿐 되돌리지 않음); (2) Knowledge 는 일방향 (수정/결정/회고는
온톨로지에 *추가만* 됨). 감독 부담은 decay 한다.

### 코드 구조 (v8 post-migration)

```
bsvibe-app/                  # repo
├── backend/                 # FastAPI monolith
│   ├── router/              # Router context — Router.invoke(LlmRequest) facade
│   ├── knowledge/           # Knowledge context — Knowledge.{ingest,retrieve_canon,settle}
│   ├── workflow/            # Workflow context — Receive → Frame → Agent → Deliver → Settle
│   ├── identity/            # Identity context — auth, tenancy, RBAC
│   ├── schedule/            # Schedule context — workspace_schedules + trigger runner
│   ├── extensions/          # Extensions context — plugin engine + skill engine
│   ├── api/v1/              # unified REST (5 split packages per Lift M1)
│   ├── mcp/<context>/       # unified MCP per context
│   └── embedding/           # hoisted shared embedding service
├── bsvibe_sdk/              # uv workspace member — Plugin-only SDK (D42)
├── plugin/<name>/           # 9 connectors (github/notion/slack/telegram/discord/email_sender/linear/sentry/trello)
│                            # + audit plugin (relocated from backend in Lift R2a)
├── skill/<name>/            # placeholder; skill engine in backend/extensions/
└── apps/pwa/                # Next.js PWA
```

**2 facade:**
- `Router.invoke(LlmRequest) -> LlmResult` — 모든 LLM 호출의 단일 진입점.
  Tier default (`pipeline==single → local provider`, `design_then_impl → executor
  provider`), 라우팅 룰, 클래스 내 정책 (D4) 모두 router 안에 캡슐화.
- `Knowledge.{ingest, retrieve_canon, settle}` — vault/canon/embedding 의
  단일 진입점.

**6 bounded context** 모두 application/domain/infrastructure 3-layer
(workflow + knowledge 만 application 내 runtime/delivery 등 sub-package).

**Repository pattern:** 6 컨텍스트 전체에 15 Repository Protocol —
모든 aggregate 가 seam 보유 (I-Repo-* lifts).

**Multi-server safety (Lift J):** settle/delivery 에 claim-or-skip leasing
(advisory lock + status-flip); agent/verifier/relay/intake 워커는 이미 advisory
lock; audit subscriber 는 `INSERT … ON CONFLICT DO NOTHING` 로 idempotent
persist.

**Defensive patterns (Lift N):** import-linter 3 contract CI gate (SDK no
backend / common leaves no bounded contexts / connector plugins only via SDK);
`__all__` discipline (33→60 `__init__.py`); 15 `# bsvibe:stable-internal`
markers (Phase-1 informational, P2 enforcement 보류); 7 context docstring
contract; Router + Knowledge facade signature golden tests.

---

## 3. 구현 상태 — v8 migration 완료

코어 기능 갭 (B1–B17, Phase 1 routing, D1–D6) 은 v8 migration 직전에 닫혀
있었고, **v8 migration 은 그 위에서 코드 구조를 재정렬**한 작업이다. 따라서
"구현 상태" 표는 v8 migration **전후 구조** 비교가 가장 의미 있다.

| 영역 | Pre-v8 (2026-05-30) | 현재 (2026-06-02) | Evidence |
|------|---------------------|-------------------|----------|
| LLM 진입점 | `provider == "executor"` 분기 8 사이트 산재 | **`Router.invoke` 단일 facade**, 분기 0 사이트 | Lift A/D — `backend/router/` |
| Knowledge 진입점 | vault/canon/embedding 호출 산재 | **`Knowledge` 단일 facade** (ingest/retrieve_canon/settle) | Lift A — `backend/knowledge/` |
| Top-level 디렉토리 | `gateway/` + `accounts/` + 루트 `routing/` 등 혼재 | **6 bounded context** + 단일 `api/v1/` + 컨텍스트별 `mcp/` | Lift B/C + 후속 |
| Executor orchestrator | `executors/orchestrator.py` 772 LOC god-file | 4 파일로 분해 | Lift D |
| Workflow runtime | `execution/orchestrator.py` 1652 LOC + 산재 `orchestrator/{workflow_sm,schema,frame,safe_mode}.py` | `workflow/application/runtime/` 7 파일 | Lift H2a/H2b/H2c/H3a-d |
| Run worker | `workers/run.py` 1445 LOC god-file | 7 파일 (74-372 LOC each), thin daemon 94 LOC | Lift §17.2a |
| Knowledge writer | `writer_core.py` 1159 LOC | 6 파일 | Lift L1 |
| Canonicalization service | 1158 LOC god-file | 7 파일 (mixin pattern) | Lift L2 |
| Ingest compiler | `ingest_compiler.py` 894 LOC | 6 파일 (per-chunk related-context invariant 보존) | Lift L3 |
| Deliverables API | `api/v1/deliverables.py` 749 LOC | 7 파일 패키지 | Lift §17.9 |
| Connector dispatch | `connector_dispatch.py` 887 LOC | 5 파일 | Lift §17.7 |
| REST handler god-files | 5 god-file (runs/inside/products/safemode/decisions) | 30 sub-file (Pattern A 분할) | Lift M1 |
| Connector 위치 | `backend/plugins/implementations/` (in-tree) | **repo-root `plugin/<name>/`** — SDK-canonical, audit 도 plugin 으로 | Lift R1/R2a/R2b |
| SDK | 없음 (Plugin 정의가 backend 안) | **`bsvibe_sdk/` 루트 sibling**, uv workspace member, Plugin-only (D42) | Lift S |
| Repository seam | aggregate 별 산발 | **15 Repository Protocol** — 6 컨텍스트 전체 커버 | I-Repo-* |
| Multi-server safety | 단일 호스트 가정 | claim-or-skip lease + advisory lock + idempotent audit persist | Lift J |
| Defensive guardrails | 없음 | import-linter CI gate (3 contract) + `__all__` discipline + facade golden test | Lift N |
| Safety YAGNI | M2 DangerAnalyzer + D3b auto-compensation + static analyzer 설계 진입 | **롤백** — 실 incident 없이 게이트 추가 안 함 | Lift 0a/0b/0c |
| **Prod e2e dogfood (design→impl 체인 + Decision + Knowledge)** | 단일 dogfood (2026-05-28) | **2026-06-02 재검증 PASS** — 아래 §4 참조 | run `9bf26f32` → `8b7aba75` |

**품질 바:** 백엔드 ~2750+ test function (출시 라운드에서 verify/F3/MCP 테스트
추가) + PWA 586 vitest; CI = PG pytest cov≥80 + ruff + mypy strict + PWA(biome
+tsc+vitest+build) + **import-linter**. Alembic head
`20260619_workspace_schedules` (KG/e2e/verify/출시full-test 라운드도 schema 추가 없음).
**Prod = main `48aadf0`** (2026-07-03, 최종 전체 E2E: verify 무결성 재설계 #481~490 배포 후
false-positive 0 실증 + report proof surface #491 = 오버플로우/검증실패 UX 수정), backend +
worker 컨테이너 up, host launchd executor(2대: mac-mini-executor + worker-admin) online, OAuth 자체관리.

---

## 4. 최근 마일스톤

### 4.-0b 실행 모델 재설계 + 검증 사정거리 (2026-07-04 ~ 08-10)

| 트랙 | PR | 상태 |
|---|---|---|
| INV-1 채널 레지스트리 | #581~#592 | ✅ 머지 |
| Notifier 배선 (파이프라인 주체이전 push→pull) | #593~#597 | ✅ 머지·배포 |
| Executor 실행모델 실체화 + 세션릭 outage 수리 | #632/#633 | ✅ |
| bootstrap private repo clone | #679/#682 | ✅ |
| 로컬 제품 R2-bundle | #667~#672 | ⚠️ 코드 완성, **prod cutover 미실행** |
| **client-attach 실행 모델** | #693~#701 | ✅ 머지 + E2E 실증 |
| **in-place verify (정직한 PROVED)** | #702~#705, #716~#718 | ✅ 머지 + E2E 실증 |
| **실행 모드 파리티** | #719~#721 | ✅ 머지 + E2E 실증 |
| 딜리버러블 조용한 절단(숫자 날조) | #722 | ✅ |
| 전달 타깃 격리 + client_attach github 스킵 | #723 | ✅ |
| **전 표면 검증 — 격리 오버레이·슬롯·기동 유도·스택 생명주기** | #724~#728 | ✅ 머지·배포 |
| **전 표면 검증 4a — 두 실행 모델이 같은 verify 를 탄다** | #729 | ✅ 머지·배포 |
| **전 표면 검증 4c — 검증 실행을 컨테이너로(툴체인이 환경을 지목)** | #730 | ✅ 머지·배포 |
| **전 표면 검증 4b — 체크가 그 환경 '안에서' 돈다** | #731 | ✅ 머지·배포 |
| 검증 docker 컨텍스트 핀이 컨테이너에 도달 안 함 | #732 | ✅ 머지·배포 |
| **전 표면 검증 5a — 표면 체크가 1급 범주(`kind:surface`)** | #733 | ✅ 머지·배포 + **prod 실증** |
| **client_attach 런이 per-run 워크트리에서 일한다** | #734 | ✅ 머지·배포 |
| **client_attach 런이 자기 작업을 커밋·push 한다 (#723 구멍 복구)** | #735 | ✅ 머지·배포 + **prod 실증** |
| **런이 끝나면 자기 워크트리를 돌려준다 (워크트리 리퍼)** | #736 | ✅ 머지·배포 + 실 트리 스모크 |
| **전 표면 검증 5b — compose 제품의 체크가 스택 네트워크 안에서 돈다** | #737 | ✅ 머지·배포 |
| **client_attach 런이 push 한 브랜치가 PR 이 된다** | #738 | ✅ 머지·배포 (라이브 미실증 — 딜리버러블 필요) |
| **죽은 런이 남긴 워크트리를 다음 런이 회수한다** | #739 | ✅ 머지·배포 |
| **제품이 자기 검증 시크릿을 선언하고, 그게 체크에 닿는다** | #740 | ✅ 머지·배포 |
| **찌꺼기 제외 pathspec 이 스테이징 전체를 거부시켰다** | #741 | ✅ 머지·배포 + **라이브 실증** |
| **client_attach 런이 끝나면 형님에게 도달한다 (딜리버러블 착지)** | #742 | ✅ 머지·배포 + **라이브 실증 (#738 도 같이 증명)** |
| **stale PR 을 그 체크아웃이 있는 머신에서 최신화한다** | #743 | ✅ 머지·배포 + 라이브 실증 |
| **최신화의 conflict 를 지우지 말고 에이전트에게 넘긴다** | #744 | ✅ 머지·배포 + 라이브 실증 |
| **에이전트가 스스로 만든 커밋도 push 한다** | #745 | ✅ 머지·배포 + 라이브 실증 |
| **결정 대기 중인 런을 wedge 로 오탐하지 않는다** | `_infra` `430b74d` | ✅ 적용 |
| **머지워치가 조용히 포기하지 않는다** | #746 | ✅ 머지·배포 |
| **🎯 트랙 C — 산문·데이터 산출물도 증거를 벌 수 있다** | #747 | ✅ 머지·배포 + **prod 실증** |
| **🎯 트랙 C1b — 단언 없는 프로브는 증거가 아니다** | #748 | ✅ 머지·배포 |

**🎯 트랙 C — 증거의 정의가 코드 모양 하나뿐이었다 (2026-08-14, prod `d9aecbf`)**

형님 제기: *"프로젝트가 코드 쓰기에 매몰된 것 아닌가. bsvibe 는 비개발 작업도 하려고 만들었는데."*
코드로 확인한 결과 **맞았고, 간극은 한 문장이었다** — 게이트 파생기는 산문 산출물을
*"the judge and demonstration paths cover it"* 이라며 비키는데(`gate_derivation.py:165`),
demonstration 경로는 `_is_code_path` 가 아니면 `None` 을 냈다. **아무도 안 맡았다.**
I2 기계 자체엔 코드 특정 개념이 하나도 없었다 — 막던 건 **입구 하나**.

| 실측 (같은 레포·같은 성격의 산문 작업) | `0bbf72eb` (이전) | `e72689e8` (이후) |
|---|---|---|
| outcome_demonstration | **null** | **`demonstrated`**, 프로브 6개 |
| honesty_grade | **D** ×2 | **B** |
| 사람 호출 | `weak_evidence_no_gate` ×2 | **0건** |
| 종료 | 취소 | **`review_ready`** |

`gate_expected` 는 양쪽 다 `true` — 조건이 아니라 **증거의 정의가 바뀌었다.**

⚠️ **급소는 규칙 1 이었다.** 산출물 텍스트를 본 플래너는 방금 읽은 문구를 grep 한다 —
모든 산문이 자동 통과, 지금의 과잉 파킹보다 **나쁘다**. 그래서 artifact 플래너는 텍스트에
**눈이 멀어** 있고(기대치는 작업 문장에서만), 판정은 **advisory** 다(벌 수만 있고 못 떨어뜨림).

⭐ **라이브가 유닛 6165 개를 또 이겼다** — 실증한 런의 프로브 6개 중 **2개가 실패할 수 없는
것**이었다(`print(...)` 는 답이 틀려도 exit 0). 등급 B 는 유효했지만 등급이 그걸 구분 못 했다.
#748 이 그 조임. **프로브가 돌았다는 것과 프로브가 무언가를 걸 수 있었다는 것은 다르다.**

**🎉 client_attach PR 수명주기 완주 (2026-08-12, run `fd7cbf14`)** — prod `50fbd32`.
`nothing_to_commit`→**`work_pushed`**(#745) → `freshen_in_place_conflict`(#743, 서버 clone 0회)
→ 충돌을 트리에 남김(#744) → 에이전트가 **머지 안에서** 해결(부모 2개 커밋 `e9a6c89`,
`merge-base --is-ancestor origin/main` = YES) → `merge_watch_merged pr_number=13`.

⭐ **라이브가 유닛 6000여 개를 세 번 연속 이겼다** — #743 배포 직후 실 런이 #744 를,
#744 직후 같은 런이 #745 를 드러냈다. 각각 **한 칸 더 가야만** 보이는 결함.
그리고 형님이 폰을 보고 짚은 두 건(#742 / 워치독 오탐)이 라이브 실증이 못 잡은 층을 잡았다.

**🎉 client_attach 전 구간 라이브 완주 (2026-08-12, run `19a99b51`)** — prod `5373386`.
검증 컨테이너에서 게이트 1차 실패 → 피드백 → 2차 `commands=3 proved=true` →
`client_attach_work_pushed run/19a99b51` → **`client_attach_deliverable_landed changed=5`** →
Safe Mode 항목 → 승인 → **PR `blas1n/BStockReport#9` 자동 생성**(`pushed_by=founder_machine`,
merge-watch 등록) + **텔레그램 dispatch**(`delivery_dispatched actions=2`).
`work_steps.proof_state=proved`(착지가 올린 게 아니라 게이트가 올렸다), 파운더 트리 clean,
워크트리 0개. **#738 을 라이브로 못 봤던 이유가 정확히 이 구멍이었다.**

딜리버러블 요약 실물(형님 폰에 가는 텍스트):
```
리포트 하단에 대상 기간 한 줄 표시 추가

바뀐 파일 5개:
- src/bstockreport/metrics.py
  …
검증: 3개 확인 통과.
```
첫 줄이 **형님 의도**(에이전트 나레이션 아님) → PR 제목이 됐고, 검증 문장은 파운더 머신에서
돈 in-place 게이트를 읽어 만들었다.

**전 표면 검증 트랙 (2026-08-10 진행분)**. 설계 SoT `docs/design/production-verification.md`.
지금까지 배선된 것: 제품마다 **일회용 검증 환경**(compose 스택 또는 선언된 툴체인으로 지은
컨테이너)을 슬롯 리스로 띄우고, run 의 체크 명령이 **그 안에서** 돈다. 호스트 실행은 이제
선언(`verify_stack: null`)의 결과로만 남는다.

이 라운드에서 실측으로 정해진 것 — **`.env` 는 검증 컨테이너에 들어가지 않는다.** BStockReport
기준 파운더 머신 FAIL / 컨테이너+`.env` FAIL / 컨테이너 `.env` 제외 **148/148 PASS**
(`test_config.py::test_defaults` 가 기대한 기본값 대신 실 Alpaca 키를 읽고 있었다). `.env` 는
앰비언트 호스트 상태이자 크리덴셜 표면이다.

같은 라운드에서 밟은 함정 둘 (둘 다 조용하다):
- **`env VAR=… cmd` 접두는 파이프라인의 첫 프로세스에만 붙는다** — 기동이
  `docker run … | docker exec …` 이라 절반만 고정됐다. shell export 로 교체.
- **`.env.prod` 에 넣어도 컨테이너에 도달하지 않는다** — `compose.prod.yaml` 이 명시
  allowlist 다. 코드·테스트·env 파일이 전부 "설정됐다"고 말하는데 도는 곳에서만 기본값(#732).

**prod 실증 완료 (2026-08-11)**: run `a2c2894a` 가 컨테이너에서 `PROVED`(`environment` 필드 기록,
컨테이너 회수 확인), run `b09f0920` 에서 파생기가 **스스로 `kind:surface` 를 발행**하고
`surface_exercised:true`. 1차 `ruff format --check` 실패 → 에이전트 피드백 → 2차 PROVED 로
**루프가 닫히는 것까지** 관측. 음성 대조도 완료 — BStockReport 하네스에 #722 회귀를 주입하면
도착 텍스트 동일성 단언이 실패한다.

**✅ 형님이 짚은 구조적 결함 — 3부작 완결**: "미커밋 변경이 있다는 것부터 잘못" — client_attach
런이 파운더 체크아웃을 **직접** 편집하고 있었다. 런의 작업 수명주기 전체가 이제 배선됐다:
**#734 워크트리를 만든다 → #735 그 안에서 커밋·push 한다 → #736 끝나면 돌려준다.**

- **#735 (prod 실증)**: run `420e7f56` 이 `origin/run/420e7f56` 로 실제 push 됐고 파운더
  체크아웃은 깨끗하게 남았다. PR 자동 생성(`open_pr`)만 의도적으로 밖에 뒀다 — #723 이 막아둔
  `resolve_github_binding` 의 client_attach 스킵을 어디까지 되살릴지 별도 판단이 필요하다.
- **#736**: 회수는 `release`(= `agent_loop` 의 `finally`)에서 — 모든 종료 경로가 지나는 유일한
  seam이다. **`--force` 를 절대 쓰지 않는 것이 안전장치 전체**다: 실 git(2.52) 실측으로
  `worktree remove` 는 수정·untracked 가 있는 트리를 거부하고 ignored 만(`.venv`) 있는 트리는
  거부하지 않는다 — 즉 그곳에만 존재하는 작업을 가진 트리가 정확히 git 이 못 지우게 하는 트리다.
  브랜치·오브젝트는 안 건드리므로 **push 가 실패한 런의 작업도 남는다**. 보류는 실패가 아니라
  자기 이름을 가진 결과(exit 2). exit 0 은 `[ ! -d ]` 로 **관측**한다 — #665 가 이걸 안 해서 샜다.

**5b 완료(#737)**: 격리 오버레이가 호스트 포트를 안 여는 대가로 **호스트 명령이 스택에 못 닿았다** —
compose 분기는 `wrap` 이 항등이라 스택을 띄워놓고 체크는 그 스택이 안 보이는 곳에서 돌고 있었다.
스택 네트워크(`<project>_default`)에 붙은 **prober** 컨테이너가 그 구멍을 메운다. 실측 함정:
`docker compose down -v` 는 엔드포인트가 붙은 네트워크를 **못 지우면서 exit 0** 을 낸다 → prober 를
먼저 회수해야 런마다 네트워크가 새지 않는다. 이것이 설계 §6 PR 4(HTTP/DB/MCP 하네스)의 전제였다.

**#738 — client_attach 가 다시 PR 을 받는다.** #723 은 github **바인딩 자체**를 껐고, 바인딩은
delivery 가 github 를 찾는 유일한 경로라서 그 모델의 런은 영원히 PR 을 못 받는 상태였다. 막아야
하는 것은 **서버가 소스를 갖는 것**이므로, 가드를 그 일이 실제로 일어나는 두 지점으로 옮겼다 —
run-setup provisioner 의 clone, merge-watch **freshness** 의 재-clone(⚠️prod 에서 auto-merge 가
켜져 있어 실경로다). 자동머지 자체는 순수 API 라 그대로 동작한다. "빈 PR 없음" 규칙은 로컬
체크아웃이 아니라 **github 에 compare 로** 묻는다 — 로컬 기록은 실제 landed 와 어긋날 수 있다.

**#739 — 고아 워크트리.** #736 은 `release` 에서 회수하는데 kill 된 런은 거기 못 간다.
**판단은 서버가(무엇이 아직 도는지), 목록은 머신이, 거부는 git 이** — 3층. 회수 시점은 다음 런의
`acquire`(#725 슬롯 리스와 같은 본능, 리퍼 워커 불필요).

**#740 — 표면 프로브의 마지막 전제.** 형님 지적: 테스트 아이덴티티·프로브 대상·단언 범위·**테스트
툴체인까지 전부 제품마다 다른 사실**이라 플랫폼이 정할 게 아니다. 툴체인은 이미 #737 이 답을 갖고
있었고(제품이 `verify_stack.image` 로 Playwright 이미지를 지목 ⇒ **BSVibe 에 Playwright 불필요**),
남은 건 **시크릿 배관** 하나였다. ⚠️exec 명령은 `executor_tasks.prompt` 에 그대로 저장되고 트림 없는
Redis 스트림에도 실리므로 `-e NAME=값` 인라인은 비밀번호를 DB 에 쓰는 것 — 값은 **명령 옆으로**
간다(per-run MCP 토큰이 쓰는 그 경로). 쓰기 seam 봉인 · 읽기 `***` · 마스크=보존(설정 화면 왕복이
시크릿을 지우는 것 방지) · 복호 실패는 drop.

**#741 — 라이브 실증이 유닛 6071개가 놓친 결함을 잡았다.** run `2abd398e` 가 게이트를 통과하고도
커밋을 못 했다: `git add -A -- . ':(exclude).venv' …` 는 그 경로가 **존재하고 또 gitignore 될 때**
스테이징 전체를 거부한다(exit 1). 조건이 **둘**이라 픽스처가 `.venv` 만 만들고 `.gitignore` 를 안
만들면 영원히 green — 앞선 E2E 도 그 디렉터리가 없어서 통과했다. 처방 = `git add -A` 후
`git reset -- <찌꺼기>`. **그리고 그 순간 #736 리퍼가 작업을 지켜냈다**(`worktree_held
reason=uncommitted_work`) — `--force` 를 안 쓰기로 한 판단이 첫 실전에서 값을 했다. 수정 후
run `7f890b10` 에서 커밋→push→워크트리 회수까지 **루프가 닫히는 것을 관측**했다.

**🔴 최우선 미해결 (2026-08-12, 형님이 짚음)**: **client_attach 런이 끝나도 형님에게 아무것도
도달하지 않는다.** 서버 sandbox 는 `run_persistence.finish_verified` 가 Deliverable +
DeliveryEventRow + settle 활동을 만드는데(그 docstring 은 이 계약이 **"compute backend 무관"**이라고
선언한다), client_attach 는 `client_attach_terminal` 로 빠져 그 헬퍼를 안 탄다. 결과: Deliverable
없음 → Safe Mode 승인 없음 → **Decision·텔레그램 없음** → **#738 PR 자동 생성도 안 됨**(PR 은
딜리버러블 전달에 딸려 있다) → settle·Brief 반영 없음. #692 의 "서버에 소스가 없으니 전달할 것이
없다"는 전제가 #735(브랜치 push)·#738(PR)로 **두 번 뒤집혔는데 이 자리만 안 따라왔다** — #723 과
같은 종류의 낡은 전제. `emit_deliverable` 툴은 제공되지만 `_SYSTEM_PROMPT` 에 그걸 내라는 말이
없어 **모델의 자발성**에 달려 있다. 형님 결정 = **플랫폼이 보장한다.** 설계·함정(⚠️proof_state 를
무조건 PROVED 로 올리면 안 됨)은 인수인계 참조.

**그 외 남은 것**: 브라우저 하네스(전제는 다 깔렸다 — 남은 건 제품 레포 쪽) · client_attach PR 의
**최신화/conflict 해결**(체크아웃이 파운더 머신에 있으므로 거기서 — #734 워크트리 + #702 exec 채널이 재료).
인수인계 `internal-docs:BSVibe_Handoff_Prompt_2026-08-12.md`.

**이 라운드의 교훈**: in-place verify는 "머지하면 끝"인 줄 알았는데 **프로덕션에서 한 번도 작동한 적이
없었다**. PR 3개가 추가로 필요했고 전부 E2E로만 드러났다. 같은 병이 반복됐다 —
**테스트가 프로덕션이 주지 않는 값을 자기가 인자로 넣어주는 것**(`acquire(id, "/founder/path")` →
인자를 되돌려주는 구현이 영원히 통과) 그리고 **버그를 명세로 박아두는 것**(`assert len(summary) == 500`
— 숫자를 날조하는 절단을 CI가 green으로 지켜주고 있었다). 스킬 [[boundary-test-must-not-supply-the-answer]].

### 4.-0a 최종 전체 E2E — UI/UX+동작 통합 (2026-07-03) — **GO**

verify 무결성 재설계(#481~490, prod `1a23b7c`) 이후 **출시 전 최종 전체 E2E**. 방법론
record→batch-fix→retest. **상세 SoT: `BSVibe_Final_E2E_Check_2026-07-03.md`.**
- **verify 정직성 정량 관측(prod DB)**: passed 검증 중 실패명령 포함 **0/35 (false-positive 0)** —
  "verified"가 실패체크 통과 이력 없음 = **verify 무결성 실증**(2026-07-01 Q-2 blocker 해소 확인).
  honesty_grade: 재설계 후 run 전부 **B**(테스트 product repo에 CI 없어 grade A 부재 = config
  artifact, 코드버그 아님). scope clean28/flagged7(I3, 차단 아님). demonstration demonstrated9/failed6.
- **founder 코어 UX 2건 수정·배포·라이브 retest (PR #491, prod main `48aadf0`)**:
  - **#1** 참고지식 오버플로우 = 긴 canon 문장이 stadium pill서 텍스트 곡선 밖(mobile 208px pill).
    → 문장형 reference는 읽기 블록(`report-chip--statement`, radius 12px). 짧은 concept칩은 pill 유지.
  - **#2** 검증 실패 wall-of-red + why/next 부재(관측: `4c621dc7` 21행 failed→3 passed인데 "Verified").
    → authoritative 1개 전면 + "N earlier attempts" 접힘; passed엔 **Evidence 등급(A~D)+I2 demonstration
    probe** 표시; failed엔 실패명령+exit+output(why)+run 열어 retry(next). PWA-only, TDD 5, CI green.
- **표면 QA**: Brief/Settings(Models 200, A-1 회귀 없음)/Knowledge 그래프 navigable/모바일 반응형 출시급.
- **후속 polish 수정 (PR #492, prod `e838e92`, 라이브 검증)**: F3 worker liveness(heartbeat 만료→Offline,
  "Online+Stale" 모순 해소) · F4 held delivery 오표시(Frame LLM이 실 request를 "No task provided"로
  오판 → degenerate frame title 스킵, real intent 표시) · F5 지역 자동 locale(첫 방문 Accept-Language,
  en fallback, 명시선택 우선; founder=en 기본 OK).
- **🟡 F6 (운영, codex)**: 기본 라우팅 executor/codex인데 dispatch 실패. 진단: 바이너리 ENOENT(재설치
  복원)→SIGKILL(cert revoke, ad-hoc 재서명 해소)→**codex 0.130.0 실행 hang**(6/28엔 정상 → codex 환경/도구
  문제, 제품 버그 아님). claude_code executor 정상. 잔여=codex 환경 수리 or 라우팅 claude_code. (port 8700은
  colima 정상 포워드 — 앞선 "충돌" 진단 오류 정정.)

### 4.-0 출시 전 완전 풀테스트 (2026-07-01) — **NO-GO (코어 품질)**

이전 라운드(§4.-1) 이후, **출시 전 최종 완전 풀테스트**를 라이브 prod로 전수 재검증
(스킵 없음, 이전 점검 항목도 전부 재확인). 방법론 record→batch-fix→retest.
**상세 SoT: `BSVibe_Prelaunch_Final_Check_2026-07-01.md`.**
- **A Playwright UI/디자인 전 surface**: 로그인/Brief/Deliverable+diff/Knowledge그래프/
  Skills/Settings(General·Models·Connectors·Notifications·Developer·Account)/Direct/
  모바일/Product(L1)/Run(L2·L3). 비주얼·구조·반응형 출시 수준.
- **B~G 라이브**: Direct 질문(native 라우팅 시 answered=true ~3s, grounding honor;
  기본 executor 라우팅은 45s graceful degrade) · Direct 작업(dispatch→verify passed→R1→
  gardening 전 루프) · **design→impl** codex(설계)→claude_code(구현) handoff **수렴**
  (round11/12 blocker 해소) · connector 인바운드 서명 webhook→run→clone(App installation
  token)→outbound tools→review_ready(held) end-to-end · 임포트 bootstrap(개념 substance
  empty 0/104·임베딩 reconcile·stub 0) · MCP OAuth 왕복+72 tool parity+실데이터+그래프
  navigable.
- **발견·수정**: 🔴 **A-1 = Settings/Models `/accounts` 500**(Local Ollama 계정의
  `data_jurisdiction='self-hosted-kr'`가 read Literal에 없어 admin ws 모델설정 전면 불능)
  → tolerant read(`_coerce_jurisdiction`→unknown) **#476 머지·배포·라이브 재검증**(200 OK).
  🟡 A-2(Brief needs-you에 shipped item held 표시 — shipped run 제외 필터 **#477** + 3
  stale행 정리) · A-3(Workspace ID=account_id → 실 workspace id, #477) · A-8(Account
  sign-in identities 빈 섹션 empty-state, #477). 전부 TDD+worktree+squash, CI green.
- **connector outbound**: `github__open_pr` **라이브 입증**(PR #413 = held run 6b03f1c5 승인→실 PR, backend/common/factorial.py, composed body). issue-close-comment(Closes #N)은 코드+유닛테스트 존재하나 **실 GitHub 이슈 필요**(forged 이슈 불가) → founder 실 이슈 생성 시 라이브 입증.
- **follow-up(비필수)**: A-4/A-5(FAB float·title cap, 설계상/cosmetic) · A-9(실패 run
  evidence detail) · D-2(impl run에 design spec seed 배선) · B-1(인라인 Q&A UX 힌트/빠른
  폴백, 암묵라우팅 금지). E-2(github 외 OAuth app founder 설정).
- **판정(정정): NO-GO** — 표면·배관은 출시급이나 **코어 산출물 품질 + verify 무결성**이 실작업서
  미달. 🔴Q-1 executor가 실 repo·실작업서 garbage(9350e71e "README 한 줄"→12파일 스퍼리어스,
  [[bsvibe-executor-subprocess-too-heavy]]) · 🔴Q-2 verify가 garbage를 "verified" 통과(L2가
  intent만 검사, scope-discipline 미검사) · 🔴Q-3 "verified" ≠ 실 CI(내부 verified인 PR #413이
  repo CI ruff 실패). = 제품이 파는 신뢰 자체 훼손 = core blocker(A-1 아님). A-1/A-2/A-3/A-8은
  표면 버그 수정(#476/#477)이지 Q-1~Q-3 미해결. **최초 GO는 오판(founder 지적 정정).**

### 4.-1 KG 재설계 + 전체 e2e + verify 하드닝 (2026-06-29 ~ 07-01)

출시 readiness(§4.0) 이후, 산출물 품질 감사 → KG 파이프라인 재설계 → 제품
핵심 사용자 여정 전체를 라이브 prod로 재검증 → 발견 회귀 일괄 수정 → executor
인증 영구화 → design→impl full-path 검증까지. 전부 TDD + CI + prod 배포.
**상세 SoT: `archive/BSVibe_Full_E2E_Findings_2026-06-30.md`** (라운드 1~12),
설계 원본 `archive/BSVibe_Knowledge_Pipeline_Redesign_2026-06-29.md`.

- **KG 파이프라인 재설계 (PR #459~465).** 추출은 GOOD인데 하류(성숙·임베딩·검색)가
  깨져 개념 그래프가 실체 없던 문제. Lift 1~5 (개념 본문 합성 · 임베딩 reconcile ·
  navigability) + R1 보고서 서술화 + 1b 개념 framing.
- **전체 사용자여정 e2e (record→batch-fix→retest).** 라이브 prod로 임포트/Direct/
  GitHub 여정 전수 검증:
  - **임포트 회귀 2건 (#466/#467):** bootstrap 경로가 KG 재설계(settle-worker만
    고침)에서 누락 → 새 개념 빈 본문(K1) + 임베딩 0(K3). fix: ingest 개념 본문 합성 +
    bootstrap 임베딩 reconcile + 빈 entity stub 생성 중단.
  - **J2 Direct 질문 (#468, #471):** `/messages/ask` 500/CORS → executor 계정으로
    misroute + graceful-fail 누락. #468 graceful degrade(절대 500 X). **#471 = 설계
    정정**: executor/LiteLLM은 dispatch에서 functionally identical이어야 → 인라인 chat이
    executor로도 dispatch되게(dispatch redis 스레딩 + 45s bound). finding: executor는
    agentic이라 knowledge Q&A엔 native 라우팅이 적합([[executor-subprocess-too-heavy]]).
  - **J3 Direct 작업:** dispatch→executor→verify→deliver→R1→gardening 전 루프 ✅.
  - **J4 GitHub 이슈→PR:** inbound ✅ 라이브 입증(prod 최초 connector발 run). outbound은
    executor 인증 고장으로 막혔다가 **인증 영구화 후 완전검증**(run review_ready + R1).
- **executor 인증 영구화 (#469/#470/#472).** 근본 = **host launchd worker의 claude
  CLI가 macOS Keychain 접근 불가 → stale `.credentials.json` 폴백 → 401** (rate/quota
  아님 — 초기 오진 정정). #472 = **worker가 OAuth 수명주기 자체 관리**(`~/.bsvibe/
  claude_oauth.json` 읽어 만료 임박 시 refresh, `ANTHROPIC_AUTH_TOKEN`으로 주입,
  single-use flock). self-sustaining, founder 후속 0. 신스킬
  `launchd-daemon-cli-keychain-auth-fallback`.
- **design→impl full-path + verify 하드닝 (#473/#474/#475).** codex 설계 → claude_code
  구현 handoff 라이브 확인(라우팅/인증 OK). 단 design verify 비수렴 발견 → 3 수정:
  - **#473** verify judge 오염: agent 자체 judge가 있을 때 retriever-added knowledge를
    merge해 함께 grade하던 것 → agent 자체 criteria만 gate(retrieved=references).
  - **#474** cooperative cancel: drive loop가 매 턴 경계서 status 재조회 → cancel 즉시
    정지(이전엔 zombie turn-loop가 budget 소진).
  - **#475** verify directive: `declare_verification`이 `uv run pytest`/`ruff format`
    유도(bare `python -m pytest`는 sandbox venv서 no pytest).

### 4.0 출시 readiness 라운드 (2026-06-03 ~ 06-24) — GO

v8 구조 위에서 "출시 가능한가"를 라이브 dogfood 로 검증하고 라스트마일을 닫은
라운드. 전부 TDD + CI green + prod 배포/라이브 검증.

- **라스트마일 수정 (PR #385~391 + 인프라).** F1 api 터널 launchd + 자가복구
  프로브 / F2 PWA 토큰 자동 refresh / F4 deliverable summary = 변경파일(raw
  narration 제거) / F6 advisory judge 환각 verdict 제거 / F8 pagerank external
  stub 제외 / F10 미설정 connector silent 죽은버튼 → 에러표시 / **host-cwd 누출
  근본수정** (`--dangerously-skip-permissions` → `--permission-mode acceptEdits`
  로 cwd confine; 하니스 상속은 의도 유지) / audit test 격리 fix.
- **verify 신뢰도 스택 (PR #392~396) — "verified" 3중 게이트.** L1a = 변경 .py
  에 ruff/format/mypy 품질바를 에이전트 contract 무관하게 강제. L2 = **별도
  verifier 가 intent 로부터 독립 acceptance 테스트 작성→샌드박스 실행** (self-
  grading 순환 차단). **무조건 ON** (안전망은 flag 아님) + **약한 모델 robust**
  (pytest exit-code 로 진짜실패 vs 깨진테스트 구분, 깨진 건 버림→false-fail 없음).
  → verified = 에이전트테스트 + 품질바(L1) + 독립테스트(L2) 통과. 불일치 시에만
  사람 리뷰 = 리뷰 최소화. deliverable 상세 페이지에 검증 명령 그대로 노출.
- **F3 그래프 navigability (PR #397/#398 + 재인덱스).** community-detection 의
  Leiden modularity resolution limit 으로 노드 48% 가 mega-community(generic
  `backend` 라벨)에 뭉침. **재귀 분할 (split-only, cap=60)** + 잠복 결정성 버그
  fix (seed 미배선) + shallow 라벨에 subarea 항상. **CPM γ 는 밀도 과적합이라
  기각** (다중 repo 5종 검증). 배포 그래프 최대 커뮤니티 391→164, navigable.
- **최종 UI/UX QA + 수정 (PR #399~402).** 전 surface dogfood → **MAJOR: 리뷰
  표면이 컨텍스트 blind** (Decisions/Brief/product-runs 가 무제목·무링크 →
  founder 가 무엇을 승인·배송하는지 모른 채 결정). 수정: `buildReviewLookup` 로
  제목+product+근거링크 thread (백엔드 무변) / Direct 제품 타깃 선택기 / 제품
  삭제 UI / Models 탭 리디자인 (점진적 노출 + Compute·Routing 2그룹). 라이브 확인.
- **MCP end-to-end 검증 + 개요 fix (PR #403).** RFC9728 discovery + OAuth 게이팅
  (401, 비-MCP 토큰 거부) + **70 `bsvibe_*` tools = PWA 전 surface parity** +
  서버사이드 실행 + **라이브 OAuth 왕복** (DCR→PKCE→consent→token→`tools/list`
  +`tools/call` 실데이터). `bsvibe_graph_community` 개요가 raw id 전부(2512,
  대부분 None) → 라벨된 navigable 만(371) 으로 fix.

**부수:** 세션 자가유지 기법 (`/api/auth/refresh` 회전토큰으로 dogfood 세션 무기한
유지, founder paste 불필요). PWA = Vercel 자동배포; 백엔드 = 컨테이너 재빌드;
executor 코드 = host launchd 재시작.

### 4.1 v8 클래스 아키텍처 migration (2026-05-30 ~ 06-02)

**범위.** `archive/BSVibe_Class_Architecture_Design_2026-05-30.md` v1→v8 (3383 lines,
이제 archive) 를 **41 PR + 3 hot-fix 로 완전 구현**. 2026-05-30 ~ 2026-06-02
한 push 윈도우. 마지막 implementation lift `ee42435`, audit refactor 후 현재
HEAD `7633822`.

### Lift 시퀀스 (v8 §13 cadence 11 phase 전부)

- **Lift 0a/0b/0c** — Safety YAGNI 롤백 (M2 DangerAnalyzer, D3b
  auto-compensation, static analyzer 전부 제거).
- **Lift A** — `Router` + `Knowledge` facade Protocol.
- **Lift B/C** — `gateway/` → `router/` rename, `accounts/` revert, 루트
  `routing/` fold.
- **Lift D** — Executor strategy collapse (8 → 0 사이트), `executors/
  orchestrator.py` 772 LOC → 4 파일, `gateway/embedding/` → `backend/embedding/`
  hoist.
- **Lift G** — `backend/extensions/` 컨텍스트 (plugins + skills merge) + audit
  move-out + ActionDispatchInterceptor / SettlementSubscriber / EventBus
  Protocol stub.
- **Lift S** — `bsvibe_sdk/` repo-root 패키지 (uv workspace, Plugin-only).
- **Lift R1/R2a/R2b** — 9 connector + audit `plugin/<name>/` 로 relocate,
  audit → EventBus rewire, PluginBuilder SDK-canonical 통일.
- **Lift H1/H2a-c/H3a-d** — Workflow bounded context 생성; `execution/
  orchestrator.py` 1652 LOC → `workflow/application/runtime/` 7 파일; legacy
  `orchestrator/{workflow_sm,schema,frame,safe_mode}.py` 흡수; intake + delivery
  흡수; workers 를 `<context>/infrastructure/workers/` 로 relocate; H2a shim
  제거; NotImplementedError 4개 채움.
- **Schedule lift** — `backend/schedule/` bounded context (6번째 컨텍스트).
- **Lift I-0** — `backend/execution/` 잔여 + `backend/supervisor/sandbox/`
  workflow 컨텍스트로 흡수.
- **I-Repo-*** — 6 컨텍스트 전체에 Repository pattern, 15 Protocol.
- **Lift §17.9 / §17.7 / §17.2a** — deliverables API / connector_dispatch /
  run worker god-file 분해.
- **Lift L1/L2/L3** — knowledge graph god-file (writer_core, canonicalization
  service, ingest_compiler) 분해; L3 는 per-chunk related-context invariant
  보존 (related-context-per-chunk skill 적용).
- **Lift J** — multi-server safety hardening.
- **Lift N-Foundation + N-Coverage** — defensive pattern rollout.
- **Lift M1/M2/M3** — universal SRP cleanup (Pattern A 분할 5 god-file → 30
  sub-file; Pattern B+C+D+E 는 이미 SRP-clean).

### Hot-fix (prod 배포 후)

- **PR #265** — `Dockerfile.backend` 에 `COPY bsvibe_sdk/` + `COPY plugin/`
  를 `uv sync` **전** 으로 옮김 (Lift S/R1 cascade).
- **PR #266 → #267** — audit subscriber 등록 누락. #266 이 두 startup path 에
  `import plugin.audit  # noqa: F401` side-effect import 추가, #267 가 명시
  `register_audit_subscriber()` 함수 호출로 supersede (side-effect import / noqa
  제거).

### 인프라 변경

- **`_infra/autodeploy.sh`** (별도 repo `blas1n/workstation`, commit
  `fb283f0`) — `bsvibe-app` 블록 추가: `docker compose -f compose.yaml -f
  compose.prod.yaml --env-file .env.prod -p bsvibe-prod up -d --build
  --force-recreate backend worker`. Postgres + Redis 는 stateful 이라 rebuild
  안 함. PWA prod 는 Vercel auto-deploy.
- **`com.bsvibe.worker` launchd** loaded on Mac Mini
  (`~/Library/LaunchAgents/com.bsvibe.worker.plist`) — 함정: example plist 의
  `/Users/{USER}/.local/bin/uv` 경로가 이 호스트에 없어서 `/opt/homebrew/bin/uv`
  로 fix. PATH 에 `/opt/homebrew/bin` + nvm node bin 포함 (codex/opencode/
  claude_code CLI 탐지용).

### Prod e2e dogfood (2026-06-02)

`e2e-hello` product 대상 단일 Direct text 가 design→impl 체인 + Decision +
Knowledge 래칫을 전부 통과:

- **Run 1 (design)** `9bf26f32` — Frame 가 `pipeline=design_then_impl` 판정,
  `routing_rule_matched: target=executor/codex, stage=design`, mac-mini-executor
  (worker_id `463f766e`) 가 dispatch 받음, codex 17s 완료, 이후
  `executor_orchestrator_needs_decision: human_review_required` → Decision 생성.
- 파운더가 PWA `/decisions` 에서 "Approve & ship" 승인 → decision row resolved
  (resolution=`ship`), design run `shipped` flip.
- **Run 2 (impl)** `8b7aba75` — D1 design→impl handoff + D1b SPEC seed 로
  auto-spawn, `executor/opencode` 라우팅, opencode 19s 완료, sandbox pytest
  verify, run `shipped` flip.
- **Artifact** — `csv2json.py` (click CLI `--input/--output/--indent`) +
  `test_csv2json.py` (CliRunner + tmp_path) 자동 commit
  (`84733c8 work: Add a small CSV-to-JSON converter ... (run-8b7aba75)`).
- **Knowledge 래칫** — 2 settle_drain → 2 garden note at
  `/app/var/vault/us-1/<workspace>/garden/seedling/`:
  - `settle-decision-resolved-…` — kind=`decision_resolution`, answer=`ship`,
    다음 cross-run D5 retrieval 용.
  - `settle-test-passes-both-files-are-in-place.md` — verified=`true`,
    `artifact_refs=[csv2json.py, test_csv2json.py]`, verified-impl 패턴 (click
    CLI, csv.DictReader, json.dump, CliRunner+tmp_path) 캡처.
- **Total elapsed:** 3 분 (~80s 사람 Decision 포함).

**결론:** 전체 design→impl executor 체인 + Decision + Knowledge 누적 + 신뢰
래칫이 migrated v8 코드 위 실 prod 에서 동작. **구조 마이그레이션이 기능을 깨지
않았다.**

---

## 5. Roadmap

코어 루프 + 신뢰 래칫 + v8 구조 + **출시 readiness 라운드 (§4.0)** 까지 끝난 지금,
남은 작업은 **(a) M3+M4 구현 lift, (b) 운영 폭 (multi-host / connector breadth /
audit relay / 운영 알림 채널), (c) 다음 단계 결정 게이트**. 상세 백로그 + 비목표
+ 파운더 결정 대기 질문은 **`BSVibe_Roadmap.md`** 단일 파일이 SoT. 이 §는 그쪽을
가리킨다.

**✅ 완료 (Roadmap §2 (1)~(6), 2026-06-03):** audit retention, **M3 Ontology
Inspect/Correct UX**, GitHub connector dogfood + R2c webhook 정리, Slack/Telegram
outbound, Knowledge import 4종(Obsidian/Notion/Claude/GPT), **M4 Fleet/Inside
Proof Surface**. **✅ 이번 라운드 (§4.0, 2026-06-24):** 라스트마일 + verify L1+L2
+ F3 + UI/UX QA + MCP 검증.

요약 (남은 것 — 상세는 `BSVibe_Roadmap.md`):
- **Near-term:** (7) self-hosting dev KPI 관측 (M4 Fleet trend-arrow 가 KPI 자체,
  4주 관측 후 외부 유저 onboarding 재평가), F1 운영 알림 채널 (founder 결정 대기),
  audit relay HTTP wire-up 결정.
- **2026-12 무렵:** (8) N-Coverage P2 marker enforcement flip (6개월 retro 후).
- **Medium-term (3–6개월):** Redis Streams 승격(필요시), 2번째 Mac Mini 호스트,
  SDK author-facing 가이드, knowledge graph UX iteration.

---

## 6. 운영 (Operations)

- **Prod:** PWA `app.bsvibe.dev` (Vercel, main 머지 시 auto-deploy); API
  `api.bsvibe.dev` (Cloudflare tunnel → Mac Mini backend `localhost:8700`).
- **배포:** `_infra/autodeploy.sh` 의 `bsvibe-app` 블록이 main 폴 → `docker
  compose -p bsvibe-prod up -d --build --force-recreate backend worker`. Postgres
  + Redis 는 stateful, rebuild 안 함. **v8 migration 은 schema 추가 없어서
  alembic head `20260619_workspace_schedules` 그대로.**
- **Executor worker:** Mac Mini 의 launchd worker **3대** (2026-08-26 실측) —
  `com.bsvibe.worker` · `com.bsvibe.worker-admin` · `com.bsvibe.worker-mac-mini-e2e`;
  ⚠️ **낡음 점검은 `bsvibe-worker staleness`** (프로세스 시작 시각 vs HEAD 커밋 시각).
  `ps` 시작 시각만으로는 기준점이 없어 판정 불가. #826 이후 재시작 명령까지 출력한다.
  이전 기록은 2대였다;
  executor ModelAccount 3 개 (`executor/claude_code|codex|opencode`).
- **⚠️ executor claude 인증 (2026-07-01~):** launchd worker의 claude CLI는 macOS
  **Keychain 접근 불가** → worker가 OAuth 자체 관리. `~/.bsvibe/claude_oauth.json`
  (seed된 OAuth creds)를 읽어 만료 임박 시 refresh, `ANTHROPIC_AUTH_TOKEN`으로
  claude subprocess에 주입(`backend/executors/worker/claude_auth.py`). self-sustaining
  (8h마다 자동 refresh). **executor 코드 변경 시 2대 worker 모두 재시작 필요**
  (`launchctl kickstart -k gui/$UID/com.bsvibe.worker{,-admin}`; docker autodeploy는
  host worker 안 건드림). codex/opencode는 파일기반 인증이라 Keychain 이슈 없음.
- **launchd 함정:** plist 예제의 `/Users/{USER}/.local/bin/uv` 경로는 이 호스트에
  없음. 실제는 `/opt/homebrew/bin/uv` 사용. PATH 에 `/opt/homebrew/bin` + nvm
  node bin 포함.
- **Prod 로그인:** `admin@bsvibe.dev`. 워크트리 레이아웃: `~/Works/bsvibe-app/`
  (`.bare` + `main` reference-only + worktrees); 브랜치 작업 시작은
  `_infra/scripts/create-worktree.sh` 로.
- **Dogfood 함정:** run 별 Direct 텍스트 다르게 (동일 → `direct_duplicate`);
  run 이 product 에 바인딩되도록 `/products/<slug>` 에서 submit; 긴 Playwright
  dogfood 중 재로그인 (short-lived JWT).

---

## 7. Doc map

루트 활성 문서 17개 (README + 16). 결정·종료된 closeout/handoff/Phase 시리즈 +
구현 완료된 설계 문서는 `archive/` 또는 git history. 인덱스 = `README.md`.

- **이 파일** — master status. 모든 세션은 여기서 시작.
- **`BSVibe_Roadmap.md`** — 단일 forward-looking roadmap.
- **전략 / spec SoT** (현행 단일제품 설계):
  - `docs/architecture/strategy-synthesis.md` — 단일제품 피벗 종합.
  - `docs/architecture/ux-design.md` — 4 surface UX.
  - `docs/architecture/workflow-backend.md` — Receive→Frame→Agent loop→ε 백엔드 설계.
  - `docs/architecture/worktree-workspace.md` — 워크트리 기반 워크스페이스 인프라.
- **실행 모델 (현행 트랙)**:
  - `docs/design/client-attach-execution.md` — client_attach 실행 모델 + in-place verify SoT.
  - `docs/design/execution-mode-parity.md` — 워크스페이스/플랫폼 툴 축 분리.
  - `docs/design/production-verification.md` — **다음 트랙.** 증명을 배포 너머로.
- **미완 작업 (열려 있음)**:
  - ~~`BSVibe_Product_Bundle_R2_Cutover_Runbook.md` — prod cutover 절차 (아직 `local`).~~
    ✅ **컷오버 완료** (2026-08-26 실측: prod `BSVIBE_PRODUCT_BUNDLE_BACKEND=s3`, R2 엔드포인트).
    ⚠️ 잔여: `/app/var/bundles` 에 14MB 가 남아 있고 최근 48h 업로드 로그 0 — 정리·동작 실증 미확인.
  - `BSVibe_Local_Product_R2_Bundle_Plan.md` — 그 계획.
  - `BSVibe_Product_Tick_MVP_Handoff.md` — product tick 트랙.
  - `BSVibe_Reality_Audit_2026-07-14.md` — 전수 현실 감사 (미해결 항목 추적).
  - `BSVibe_Chat_Executor_Parity_Audit_2026-07-14.md` — executor 3종 중 1종만 파리티 확보 상태.
- **별도 프로젝트**: `BStockReport_Design.md` · `BStockReport_Progress.md` · `bloasis/` · `BStalk3r/`.

**최근 archive (2026-08-10 — 정리 22건):** 소비된 handoff 5건(2026-07-14 · 07-23 · 08-06 · 08-07 ·
BStockReport) · verify 재설계 시리즈 4건 · 출시 전 E2E/체크 3건 · 구현 완료 설계 4건(Executor Remote
Tools · NL Native Routing · Agent Authored Knowledge · Worth-Remembering 2건) · INV-1 채널 레지스트리
계획 · Notifier 배선 handoff · 통합 라우팅 handoff · Half-Wired 감사(Reality Audit로 통합, 자체 SUPERSEDED 표기).
