# BSVibe — Workflow & Backend (Working)

> 날짜: 2026-05-20
> 상태: **WORKING — 잠금 아님.** UX 패스의 다운스트림. 그린필드가 아닌
> 백엔드 진화 (엔진 코어 이미 존재 — BSNexus / BSage / BSGateway /
> BSupervisor). 대화 전반에 걸쳐 구축됨; 결정 사항은 §6 에서
> [locked] / [open] 로 표기.
> 업스트림: `docs/architecture/strategy-synthesis.md` (전략 SoT),
> `docs/architecture/ux-design.md` (UX 표면).

---

## 0. 이 문서가 무엇인가

구체적인 UX 표면에서 재도출된 워크플로우, 그리고 그것을 구동하는 백엔드
토폴로지. 시너시스 §14 워크플로우는 잠정 스케치였음 — UX 가 도출 가능할
만큼 정착된 지금 이를 대체.

(시너시스에서 가져온) 제약:
- 기존 엔진 코어 4 개는 유지. 그린필드 재작성 금지. 진화 + 글루.
- 파운더 표면은 최대한 단순. 내부 스테이지는 파운더에게 비가시.
- 신뢰 래칫(trust ratchet)이 척추 — Sage → Supervisor 루프는 실제 흐름이어야 함.
- BSGateway 라우팅 (단순 → 로컬 LLM · 실질 → opencode 구독 베이스라인 ·
  다중 계정).
- Safe Mode (BSage) 는 비-파운더 트리거와 위험한 영속 출력의 자율성을 통제.

---

## 1. 워크플로우 — 3 + ε 스테이지

각 스테이지는 명시적 입력/출력과 그것을 생산하거나 관찰하는 UX 표면을 최소
하나 가짐. 스테이지 0–3 은 내부; ε 는 루프 종단. 파운더는 오른쪽 열의
표면만 봄.

| # | 스테이지 | LLM? | 입력 | 생산물 | UX 표면 |
|---|---|---|---|---|---|
| 0 | **Trigger** | ✗ | Direct 작성 · Connector inbound · Schedule · Decision resolution | Trigger 이벤트 | Direct (1) · Triggered (2) |
| 1 | **Receive** | ✗ | Trigger 이벤트 | 영속화된 이벤트 + Product 해석 (Resource 바인딩 경유) + per-resource 필터 통과 + 후보 BSage 컨텍스트 프라이밍 + 소스 메타데이터로부터의 artifact-type *힌트* | Brief 레인 "↑ just triggered" |
| 2 | **Frame** | ✓ cheap | Trigger 페이로드 + 프라이밍된 BSage 컨텍스트 | 프레이밍된 Direction · path branch (rule-only / knowledge-only / agent-loop) · Skill 매칭 (있다면) | Direct 해석 카드 · Triggered "framed it as" 블록 |
| 3 | **Agent loop** ↻ | ✓ heavy | 프레이밍된 Direction · Resource 바인딩 · Skill 템플릿 (있다면) | RunAttempt — 반복 plan → act → verify → (continue / ask / done). **사이드 채널로 Deliver 및 Settle 이벤트를 지속 방출**. **Verify-declaration 시점:** work LLM 이 contract 를 선언; BSage 검색 (단일 가중 쿼리)이 선언과 함께 관련 캡처 패턴을 합성. 결합된 contract 가 verify 가 실행하는 것. 루프 중간의 AskUserQuestion 은 Decision 을 생성하고 일시정지; 해소되면 재개. | Brief/Product "in flight" 레인 · Decisions inbox + Decide 상세 · Delivery Report Proof 섹션 · Inside 그래프 (Settle 이벤트 도착 시 라이브) |
| ε | (루프 종료) | — | — | **verified** (성공 — 최종 방출) · **needs_decision** (일시정지; terminal 아님 — 해소 시 루프 재진입) · **system_error** (드묾 — 프로세스 사망 / 인프라 실패) | Delivery Report 에 표시되는 Run 종단 상태 |

agent loop 가 전 구간 동안 방출하는 지속 **사이드 채널**:

- **Deliver 이벤트** — 부분 커밋, 코멘트, 페이지 편집, 드래프트. 각각
  자체의 `artifact_type` 과 `channel` 을 가짐. Per-Resource `output_mode`
  가 Safe Mode 큐 vs 직접 전송을 결정. 루프당 다중 아티팩트가 예외가
  아닌 규범 (단일 루프는 보통 PR + issue 코멘트 + 문서 페이지 + … 를 방출).
- **Settle 이벤트** — Direction · 프레이밍 · Decisions+이유 · run trace ·
  관찰된 컨벤션 · 새로운 canonical 패턴. BSage 가 온톨로지에 기록.
  Inside 그래프는 라이브 업데이트.

### 1.1 흐름 다이어그램

```
                           ┌─── Decision resolution re-enters loop
                           │
                           ▼
   Trigger ▶ Receive ▶ Frame ▶ Agent loop ↻ ───────▶ verified | needs_decision
                                  │
                                  ├──── Deliver events ─▶ Delivery Gateway
                                  │                       (Safe Mode queue or direct)
                                  │
                                  └──── Settle events ──▶ BSage write
                                                          (canonicalization,
                                                           graph mutation)
```

매 스테이지에서 병행: **BSGateway** 가 모델 호출을 공급 (계층 라우팅).
**BSupervisor** 가 audit 로그를 실행. **BSage** 는 work LLM 의 컨텍스트
소스이자 Settle 이벤트의 도착지. **BSNexus** 가 상태 머신을 소유하고
agent loop 를 실행.

### 1.2 스테이지별 핵심 노트

- **Receive 와 Frame 의 경계.** Receive 는 *메타데이터* 전용 — LLM 없음,
  페이로드 의미론 없음. Frame 이 첫 LLM 호출 — 페이로드 의미. Receive
  로부터의 artifact-type 힌트는 *사전 확률(prior)* 일 뿐; Frame 이 정제하고,
  agent loop 가 작업 중 실제 artifact 타입(들)을 *선언*.
- **Frame 경로 분기.** "Knowledge-only" 답변은 agent loop 를 건너뜀 —
  BSage 가 온톨로지에서 직접 답변, output = chat (UX 시너시스 §1.3
  "fluid input"). 총 LLM 호출 1 회. 실질 비용 절감.
- **Skill 템플릿 주입.** Frame 이 Skill 에 매칭되면 (예: "한 줄에서 PRD"),
  YAML+프롬프트 템플릿이 agent loop 의 plan 을 재구성.
- **Verify 는 한 스텝, 두 소스.** 루프 내 verify 스텝은 선언 시점에
  (a) 이번 반복에서 work LLM 이 선언한 것과 (b) 동일 신호에 대해 BSage
  검색이 표면화한 것으로부터 조립된 *하나의 결합된 contract* 를 실행. 별도의
  "ratchet check" 레이어 없음 — 전부 한 contract. mechanical-wall 동작은
  신호가 발화할 때마다 BSage canonicalization 이 동일한 canonical 패턴을
  검색하는 것에서 자연 발생.
- **다중 아티팩트가 규범.** 각 Deliver 이벤트는 자체 `artifact_type` 을
  가짐; agent loop 는 실행 중 artifact 타입을 피벗할 수 있음 (예: "코드인 줄
  알았는데 결국 issue 코멘트였다"). 전역 artifact-type 약속 없음.
- **트랜잭션 컨테이너로서의 Safe Mode.** Safe Mode 큐는 per-Run — 루프
  실행 중 부분 Deliver 이벤트가 누적. 파운더가 큐 전체를 함께
  승인/거부/재프레이밍. Direct 모드 출력 (이미 외부에 나간 것) 은 롤백할
  경우 명시적 compensation 핸들러 필요.
- **`abandoned` 는 정상 terminal 이 아님.** 멈춘 루프는 abandon 이 아닌
  Decision 이 됨. `abandoned` / `system_error` 는 운영상 예외 슬롯.

---

## 2. 백엔드 토폴로지 — 단일 제품, 단일 모노레포

**전제:** BSVibe 는 *하나의 제품*. 이전의 "별도 서비스 4 + 공유 라이브러리
4 + 프론트엔드 4 + 중앙 auth + 마케팅 사이트" 산개는 *4-제품 시대의 잔재*.
배포 분리가 진정 필요한 최소로 축소. 엔진 4 개의 역할 코드는 단일 백엔드
모노레포의 내부 모듈이 되고, 중앙 auth 서버와 공유 라이브러리는 통합.

### 2.1 도메인과 배포 단위 — 총 2 개

```
EXTERNAL
  bsvibe.dev          → Marketing site (Notion-style: CTAs only, no login)
                         "Sign up" / "Log in" → app.bsvibe.dev

  app.bsvibe.dev      → BSVibe — the product (full stack on one domain)
                         · PWA (Next.js) at /
                         · API at /api/*  (auth · BFF · workers)

THIRD-PARTY (kept, may swap later — see §7)
  Supabase            → IdP. ES256 JWT + JWKS. BSVibe backend calls it
                         directly; no in-house auth-server wrapper.
```

**배포 단위:**
1. **`bsvibe-site`** — static / CDN. 마케팅 페이지만. 로그인 버튼은
   `app.bsvibe.dev/login` 으로 링크.
2. **`bsvibe-app`** — Next.js 풀스택 (PWA 프론트엔드 + FastAPI 백엔드가
   동일 도메인 공유). Docker 이미지 1 개 (또는 Vercel 의 프론트엔드 +
   컨테이너의 백엔드면 2 개) 이지만 단일 레포, 단일 릴리스 케이던스.

끝. ~~`auth.bsvibe.dev`~~ 폐지. ~~별도 제품 백엔드 4 개~~ 폐지.
~~별도 프론트엔드 4 개~~ 폐지. ~~퍼블리시된 공유 라이브러리 8 개~~ 는
모노레포에 통합.

### 2.2 백엔드 모노레포 레이아웃

```
bsvibe-app/                         ← single repo, single deploy
├─ apps/
│  └─ pwa/                          single frontend (Next.js, desktop + mobile)
│     ├─ Brief · Product · Run · Decisions · Inside · Skills · Settings
│     └─ uses internal i18n + ui packages
├─ backend/                         FastAPI monolith (one Python codebase)
│  ├─ api/                          HTTP handlers (the "BFF"):
│  │     auth · workspace · product · decisions · skills · settings
│  ├─ auth/                         Supabase wrapper (former auth-server fold-in)
│  ├─ orchestrator/                 workflow SM (Receive→Frame→Agent loop→ε)
│  │                                + per-Run Safe Mode queue
│  │                                + Deliver/Settle event fan-out
│  ├─ intake/                       trigger normalization (Direct · Connector
│  │                                inbound · schedule · Decision resolution)
│  ├─ delivery/                     artifact-type-aware render + channel routing
│  ├─ skill_matcher/                Skill template selection at Frame
│  ├─ execution/                    (was BSNexus role code)
│  │     state machine · agent loop · DinD sandbox · tool loop
│  │     · verification contract · decomposer · verifier worker
│  ├─ knowledge/                    (was BSage role code, sans plugins/skills)
│  │     vault graph · canonicalization · MCP server · weighted retrieval API
│  ├─ plugins/                      **top-level** — was BSage's, promoted
│  │  ├─ base.py / decorator.py / loader.py / runner.py
│  │  ├─ analyzer.py                DangerAnalyzer (AST is_dangerous)
│  │  └─ implementations/           Connectors (GitHub · Notion · Slack ·
│  │                                Telegram · Email · Drive · Figma · …)
│  ├─ skills/                       **top-level** — was BSage's, promoted
│  │  ├─ loader.py                  BSage SkillLoader (frontmatter shrunk)
│  │  ├─ runner.py                  invoke_skill tool — body injection +
│  │  │                             BSage retrieval prime + allowed_tools gate
│  │  └─ library/                   workspace .md skills
│  ├─ gateway/                      (was BSGateway role code + bsvibe-llm)
│  │     LLM dispatch · multi-account · 2-tier routing · usage tracking
│  ├─ supervisor/                   (was BSupervisor role code)
│  │     sandbox script execution · audit log
│  ├─ workers/                      worker process entrypoints
│  │     agent-loop · verifier · settle subscriber · delivery dispatcher
│  ├─ data/                         ORM · migrations
│  └─ shared/                       former bsvibe-authz · bsvibe-fastapi etc.
│                                   absorbed as internal utilities
└─ tools/
   └─ cli/                          founder/admin CLIs (imports backend directly)

bsvibe-site/                        separate repo, static / CDN
└─ apps/site/                       marketing pages, blog
```

프로세스 모델 (이미지 1 개, 프로세스 타입 다수):
- `api` — HTTP 서버 (FastAPI)
- `worker-agent` — agent loop 러너
- `worker-verifier` — verifier (조립된 contract 를 샌드박스에서 실행)
- `worker-settle` — BSage write 구독자
- `worker-delivery` — Delivery 디스패처
- `worker-intake` — Connector inbound 웹훅 수신기

동일 코드베이스, 다른 엔트리포인트. 모든 프로세스 타입이 데이터 레이어를 공유.

### 2.3 데이터 — 당분간 단일 스택, 단일 리전

| | |
|---|---|
| **Postgres** | 단일 데이터베이스, 멀티테넌시용 `workspace_id` 컬럼 + 심층 방어용 Postgres RLS (§6 #3) |
| **Vault FS** | 단일 트리, per-workspace 하위 디렉터리; BSage 지식의 markdown SoT |
| **Vector** | 단일 스토어; per-workspace 파티션 |
| **Redis Streams** | 단일 클러스터; 모든 내부 이벤트 스트림 |
| **Object store** | 단일 버킷; 아티팩트, audit 로그 |
| **Supabase** | 외부 IdP (교체 연기 — §6 [parked]) |

**초기 배포: 자체 호스팅 Mac mini.** 단일 물리 리전. 현재 4-제품 스택과
동일한 패턴 (Docker compose, autodeploy, Caddy). `Workspace.region` 초기값
`self-hosted-kr` (파운더 위치). 데이터 레이어 추상화 (`db_for(region)`,
`vault_for(region)` 등) 는 Day 1 부터 자리잡혀 있으나 현재는 모두 단일
인스턴스 반환 — **멀티 리전은 아키텍처상 준비됨, 인프라는 연기** (§8).

### 2.4 무엇이 어디에 있었는가 → 이제 어디로

| 과거 | 이제 |
|---|---|
| BSNexus 제품 (backend + frontend + auth + tenant + PG) | 역할 코드 → `backend/execution/`. Frontend, auth, tenant, PG — *드롭* (흡수) |
| BSage 제품 (backend + frontend + auth + tenant + PG) | 역할 코드 → `backend/knowledge/`. Frontend, auth, tenant, PG — *드롭* |
| BSGateway 제품 (backend + frontend + auth + tenant + PG) | 역할 코드 → `backend/gateway/`. Frontend, auth, tenant, PG — *드롭* |
| BSupervisor 제품 (backend + frontend + auth + tenant + PG) | 역할 코드 → `backend/supervisor/`. Frontend, auth, tenant, PG — *드롭* |
| `bsvibe-auth` (auth.bsvibe.dev 서버) | `backend/auth/` 로 통합 (얇은 Supabase 래퍼) |
| `bsvibe-authz` lib | `backend/shared/authz/` 또는 `backend/api/auth/` 로 통합 |
| `bsvibe-fastapi` lib | `backend/shared/fastapi/` 로 통합 |
| `bsvibe-llm` lib | `backend/gateway/llm_client.py` 로 통합 |
| `cli-base` lib | `tools/cli/base.py` 로 통합 |
| `@bsvibe/i18n` lib | 내부 모노레포 패키지 (퍼블리시 없음), `apps/pwa/` 에서 사용 |
| `bsvibe-site` (마케팅) | **별도 유지** — 별도 레포, CDN 배포, 로그인 UI 없음 |

---

## 3. 데이터 모델 — 신규/변경 핵심 엔티티

```
User (1) ──── (n) Membership ──── (n) Workspace
                  role: "owner" | "admin" | "editor" | "viewer"
                  invited_by_user_id: UUID | null
                  joined_at, left_at (soft delete)
                  # v1 operates 1:1 with role=owner. Schema is N:M-ready
                  # so team workspaces ship without a migration later.

Workspace (1) ─── region (TEXT, default 'self-hosted-kr')
                  legal_basis (ENUM 'contract' | 'consent', default 'contract')
                  deleted_at (TIMESTAMP, soft delete; hard delete after 30d)
              ────── (n) Product ─────── (n) Resource
                          │                    │
                          │                    └── ConnectorAccount (FK)
                          │                        + resource_id (in Connector's terms)
                          │                        + trigger {enabled, filters}
                          │                        + output_mode {safe|direct}
                          │
                          └── Direction → Request → WorkStep → RunAttempt → Deliverable
                                              │
                                              ├── Decision (0..n)
                                              └── BSage write events (n)

Workspace (1) ─── (n) ConnectorAccount (= the "Settings · Connectors" rows)
                          │
                          └── (specific to provider) credentials

Workspace (1) ─── (n) ModelAccount (the "Settings · Models · Add" rows)
                          │
                          ├── provider, label, credentials (KMS-encrypted), account_id
                          └── data_jurisdiction (declared by the worker SDK at
                              registration — never inferred, never user-typed)

Workspace (1) ─── (n) Skill (YAML frontmatter + prompt body)
                          │
                          └── visibility {just_me, workspace}

BSage (per workspace) ─── (n) Knowledge node
                                 │
                                 ├── content (markdown SoT)
                                 ├── provenance (Direction/Decision/Run that produced it)
                                 ├── canonical_refs (typed-action canonical anchors, if any —
                                 │      these are what give a node its wall-like retrievability)
                                 ├── applies_to {Product? | global}
                                 └── retracted {by founder, at}

Safe Mode queue (per Run) ─── (n) QueueItem
                                 │
                                 ├── deliver_event_id (FK), workspace_id, run_id
                                 ├── created_at, expires_at (= created_at + 90d)
                                 ├── status: "pending" | "approved" | "rejected" |
                                 │            "archived_expired" | "deleted"
                                 ├── extensions_used: int (max 2; each +30d, cap +60d)
                                 └── archived_at, deleted_at
                                 # 90d active → archived (still viewable) → +30d → hard delete
```

핵심 변화:
- **Product 가 1급 객체.** 오늘날 BSNexus 는 workspace == 제품 컨텍스트로
  암묵적 가정. Product 를 자체 엔티티로 승격하면 L1 표면과 per-Product
  지식 슬라이스가 열림.
- **Resource** 는 per-Product × Connector 바인딩. "selection + trigger +
  output_mode" 3-knob (UX §6 v2.2) 가 여기에 거주.
- **ConnectorAccount** vs **ModelAccount** — 별개. Connector = 외부
  *시스템* (데이터 read/write). Model = LLM *계정* (compute 공급). 둘 다
  동적 Add/Remove 목록이지만 BSGateway 는 ModelAccount 만 신경 씀.
- **knowledge 노드에 `tier` 컬럼 없음.** BSage 는 단일 가중 검색 스토어;
  mechanical-wall 동작은 canonicalization 에서 옴 (canonical typed-action 에
  부착된 노드는 그 신호가 발화할 때마다 결정론적으로 표면화). Guard 테이블
  없음, promotion 파이프라인 없음.

### 3.1 횡단 이벤트 스키마 — 잠금

워크플로우 스테이지를 가로지르는 세 가지 이벤트 타입. Pydantic 모델,
`backend/intake/schema.py` (TriggerEvent) 및 `backend/delivery/schema.py`
(DeliveryResult, ActionResult) 에 정의. 모든 emit/return 경로에서 검증.

#### TriggerEvent — 워크플로우 진입 표준 형태

```python
class TriggerEvent(BaseModel):
    # Identity
    id: UUID
    workspace_id: UUID
    received_at: datetime

    # Source classification
    source: Literal["direct", "schedule",
                    "decision_resolution", "connector_inbound"]

    # Connector-specific (source = connector_inbound)
    connector: str | None                  # plugin name ("github" / "notion" / ...)
    connector_account_id: UUID | None      # which specific account
    resource_id: str | None                # plugin-specific identifier
                                           # e.g. "bsvibe/bsvibe-site#42"

    # Routing hints (Receive populates from metadata; Frame refines via LLM)
    product_id: UUID | None
    suggested_artifact_type: str | None    # "code" / "page" / "image" / ...
    suggested_skill: str | None            # for schedule-fired or direct-named skill

    # Content
    intent_text: str | None                # natural-language ask
                                           # · direct: founder typed
                                           # · connector_inbound: extracted from payload
                                           # · schedule: usually empty
                                           # · decision_resolution: original Direction
    raw_payload: dict                      # original event (audit / replay)

    # Idempotency
    idempotency_key: str | None            # webhook dedup (X-GitHub-Delivery, etc.)

    # Provenance
    actor: Literal["founder", "external", "system"]
    correlation_id: UUID | None            # links re-dispatched / related triggers
```

#### DeliveryResult — 플러그인 outbound 결과 (Deliver 이벤트 1 개와 1:1)

```python
class DeliveryResult(BaseModel):
    # Identity
    deliver_event_id: UUID
    run_id: UUID
    workspace_id: UUID

    # Bundle (optional — atomic group key for multi-event releases)
    bundle_id: UUID | None                 # agent loop may stamp same id on
                                           # related Deliver events (e.g. a
                                           # release: PR + tag + Slack + Notion).
                                           # Safe Mode UI may group/approve
                                           # together. plugin compensation is
                                           # still per-event.

    # Outcome
    status: Literal["delivered", "queued_for_approval",
                    "failed", "retracted"]

    # Artifact identity (1:1 — exactly one external)
    artifact_type: str                     # "pr" / "issue_comment" / "notion_page" / ...
    artifact_summary: str                  # founder-readable one-liner
    external_ref: str | None               # plugin-canonical id (compensation key)
                                           # e.g. "github://bsvibe/bsvibe-site/pull/15"
    external_url: str | None               # human-clickable
    delivered_at: datetime | None

    # Safe Mode queue (when status = queued_for_approval)
    queue_item_id: UUID | None

    # Compensation (filled when status = delivered, for later revert)
    compensation_handle: dict | None       # plugin-private revert token
                                           # plugin's compensate(handle) consumes it

    # Failure
    error_kind: str | None                 # "auth" / "network" / "validation" / "rate_limit"
    error_message: str | None
    retryable: bool = False

    # Processing record (GDPR Art. 30)
    plugin: str
    data_jurisdiction: str                 # from plugin's SDK metadata
    completed_at: datetime
```

**다중 아티팩트 규칙:** Deliver 이벤트 1 개 = 외부 아티팩트 1 개. 논리적
work 스텝이 N 개 아티팩트를 만들면 (예: release = PR + tag + Slack +
Notion), agent loop 는 Deliver 이벤트 N 개를 방출. 각각 자체 DeliveryResult,
자체 Safe Mode 큐 행, 자체 compensation 핸들을 가짐. 파운더가 그룹으로
봐야 할 때만 `bundle_id` 사용.

**플러그인 내부 멀티콜:** 단일 아티팩트가 플러그인이 여러 외부 API 호출을
하도록 요구하는 경우 (예: Notion parent + children), 플러그인이 이를 은닉
— `external_ref` 는 primary 를 가리키고; `compensation_handle` dict 는
플러그인이 완전히 revert 하는 데 필요한 모든 내부 id 를 운반.

#### ActionResult — `@p.action` 동기 툴 호출 결과

DeliveryResult 와 구별됨: action 은 agent loop 가 await 하고 다음 plan 에
공급하는 *동기 툴 호출* 인 반면, Deliver 이벤트는 *비동기 사이드 방출*.

```python
class ActionResult(BaseModel):
    # Identity
    action_call_id: UUID
    run_id: UUID
    workspace_id: UUID

    # What was called
    plugin: str
    action_name: str
    args: dict                             # arguments LLM passed (audit)
    is_dangerous: bool                     # DangerAnalyzer verdict

    # Outcome
    status: Literal["success", "failure", "needs_approval"]
                                           # needs_approval = dangerous + Safe Mode

    # Result payload (consumed by LLM in next plan)
    result: dict | None                    # JSON-serializable plugin return value

    # Approval gating (status = needs_approval)
    approval_request_id: UUID | None       # surfaces as a Decision-like prompt

    # Failure
    error_kind: str | None
    error_message: str | None

    # Timing & audit
    started_at: datetime
    completed_at: datetime | None
    data_jurisdiction: str | None          # if action made external calls
```

---

## 4. 이벤트 흐름 (Redis Streams)

스테이지 전이가 이벤트를 방출; 각 코어의 consumer-group 워커가 반응.
BSNexus 가 `verification:queue` 에 이미 사용하는 것과 동일 형태 (STATUS.md).

| 스트림 | 생산자 | 소비자 | 목적 |
|---|---|---|---|
| `trigger:in` | Intake Gateway | Orchestrator | 정규화된 트리거 (어떤 소스든) |
| `direction:framed` | Orchestrator (Frame 후) | BSNexus agent-loop 워커 | Frame → Agent loop 진입 |
| `loop:tool_event` | Agent-loop 워커 | Run 진행 SSE 소비자 | Brief / Run 뷰용 라이브 반복별 내레이션 |
| `loop:verify_run` | Agent-loop 워커 | Verifier (BSupervisor 샌드박스 내) | verify 스텝당 1 개 — 조립된 contract 실행 |
| `deliver:event` | Agent-loop 워커 (루프 중) | Delivery Gateway | 지속 사이드 방출 — 부분 커밋, 드래프트, 페이지 편집. 각각 artifact_type 과 channel 운반. |
| `settle:event` | Agent-loop 워커 (루프 중) | BSage write 구독자 | 지속 사이드 방출 — 관찰된 컨벤션, decisions+이유, run-trace 너겟. BSage 가 canonicalize 후 기록. |
| `decision:pending` | Agent-loop 워커 (AskUserQuestion 시) | Frontend SSE + Notifications 채널 | 루프 일시정지; 파운더 UI 가 점등 |
| `decision:resolved` | Frontend | Orchestrator → Agent-loop 워커 | 파운더 답변과 함께 루프 재개 |
| `loop:terminal` | Agent-loop 워커 | Orchestrator | verified / needs_decision / system_error |
| `delivery:safemode_queued` | Delivery Gateway | Frontend SSE (Brief 레인 "needs you") | output_mode = safe 일 때 |
| `audit` | 모든 스텝 | BSupervisor audit 로그 | 상시 가관측성 |

주의: `deliver:event` 와 `settle:event` 는 agent loop 가 Run 전체에 걸쳐
방출하는 두 **지속 사이드 채널** — 다중 아티팩트와 지속 지식 쓰기가
예외가 아닌 규범. 루프 종료 후의 "deliver 스테이지" 나 "settle 스테이지"
이벤트는 없음 — 루프가 종료되고 그 스트림의 최종 방출이 단순히 마지막
배치.

라이브 UX 상태용 SSE (Brief 레인, Run 진행, Decisions inbox 카운트) —
폴링 없음.

---

## 5. 프론트엔드 & auth — 제품 안으로 통합

- **PWA** 는 `bsvibe-app` 모노레포의 `apps/pwa/` 에 거주. Next.js, ES256
  JWT auth, API 와 동일 도메인 (`app.bsvibe.dev`).
- **Auth** 는 BSVibe 백엔드 자체. 라우트: `/api/auth/login`,
  `/api/auth/oauth/<provider>/callback`, `/api/auth/refresh`,
  `/api/auth/logout`. 내부적으로 Supabase (IdP) 호출. `auth.bsvibe.dev`
  서브도메인 없음.
- **마케팅 사이트** (`bsvibe.dev`) 가 유일한 다른 배포; CTA 만 있고
  로그인 없음. "Log in" / "Sign up" 버튼은 `app.bsvibe.dev/login` 으로 링크.
- **기존 프론트엔드 4 개** 는 Phase 4 (§7) 까지 deprecate — 컷오버 동안
  read-only 참조, 이후 폐지.

---

## 6. 결정 상태

### [locked] (업스트림 UX / 시너시스 + 이번 라운드)
- **단일 제품, 단일 모노레포.** 엔진 4 개의 역할 코드는 내부 모듈이 됨
  (`backend/execution/`, `backend/knowledge/`, `backend/gateway/`,
  `backend/supervisor/`, `backend/plugins/`). 퍼블리시된 공유 라이브러리
  없음 — 이전 `bsvibe-*` 라이브러리 전부가 `backend/shared/` 나 해당 모듈로
  통합. BSage 플러그인 시스템은 최상위 `backend/plugins/` 로 승격
  (구 `bsage/plugins/`) — intake, delivery, knowledge 가 사용.
- **배포 단위는 2 개만**: `bsvibe-app` (PWA + 백엔드, 단일 도메인
  `app.bsvibe.dev`) + `bsvibe-site` (`bsvibe.dev` 의 마케팅).
- **Auth 통합.** `auth.bsvibe.dev` 폐지. 제품 백엔드가
  `app.bsvibe.dev/api/auth/*` 에서 auth 처리, Supabase 직접 호출. Notion
  모델 — 마케팅 사이트는 CTA 만, 로그인은 제품 도메인에 거주.
- Product 는 1급 객체; Resource 는 그것의 per-Connector 바인딩.
- ConnectorAccount 와 ModelAccount 는 별개의 동적 목록.
- **BSage 는 단일 가중 검색 스토어, tier 없음, Guard 엔티티 없음.**
  mechanical wall 은 canonicalization (신호 발화 시 canonical 노드의
  결정론적 검색) 에서 옴, 별도의 enforcement 스토어나 promotion
  파이프라인이 아님.
- **Ratchet 은 *속성* (agent loop 의 일방향 + BSage 단조 누적) 이지
  엔티티가 아님.** `ratchet:promoted` 이벤트 없음.
- **워크플로우 형태: Receive → Frame → Agent loop (지속 Deliver / Settle
  사이드 방출과 함께) → ε.** Deliver 와 Settle 은 terminal 스테이지가 아님
  — 루프가 지속 공급하는 스트림.
- **Verify 는 조립된 contract 하나를 가진 한 스텝** — work-LLM 선언 +
  BSage 검색, 선언 시점에 병합. 별도의 guard check 없음.
- **Run 당 다중 아티팩트가 규범.** 각 Deliver 이벤트는 자체
  `artifact_type` 운반. Artifact 타입은 루프 중간에 피벗 가능.
- **`abandoned` 는 정상 terminal 이 아님** — 멈춤 → Decision. 운영상
  예외는 `system_error` 만 커버.
- BSage Safe Mode 가 자율성 메커니즘 (per-Run 큐 + pull 승인).
- Safe Mode 는 부분 Deliver 이벤트의 per-Run 트랜잭션 컨테이너.

### [open] — 남은 결정

1. ~~Orchestrator 배치.~~ **폐기** — 단일 모노레포, Orchestrator 는
   `backend/orchestrator/` (내부 모듈 + 자체 워커 프로세스).
2. ~~Ratchet 흐름.~~ **폐기** — BSage canonicalization +
   verify-declaration 검색. §1.2, §2.2, §3 참조.

3. ~~멀티 테넌시 / Workspace 모델.~~ **(a) + GDPR L1 잠금.**
   - 단일 PG 스키마, 모든 루트 엔티티에 `workspace_id` 컬럼, + 심층
     방어용 Postgres RLS.
   - 3 레이어 방어: 요청 컨텍스트 미들웨어 → ORM 자동 필터 → DB 에서
     강제되는 RLS.
   - GDPR L1 Day 1 (§8 참조): `Workspace.region` + `legal_basis` + soft
     delete; `ModelAccount.data_jurisdiction` 는 worker-SDK 등록 시 선언
     (추론 없음, 사용자 타이핑 없음); 삭제 / 내보내기 / processing-record
     API; sub-processor 공시 페이지; 시크릿 KMS 암호화; PII 컬럼 태깅.
   - 초기 배포: 자체 호스팅 Mac mini, 단일 리전 (§2.3).
   - 멀티 리전 인프라는 연기; 데이터 레이어 추상화는 Day 1 부터
     아키텍처상 준비 완료 (§8 L2).

4. ~~Intake 형태.~~ **잠금 — BSage 플러그인 시스템 재사용, 최상위
   `backend/plugins/` 로 승격.**

   BSage 소스 (`~/Works/BSage/main/`) 대조 확인됨: 기존 `@plugin`
   데코레이터 + 3-카테고리 (input/process/output) + 5 트리거 타입
   (cron/webhook/on_input/write_event/on_demand) + 양방향
   `@execute.notify` + `@execute.setup` credentials + DangerAnalyzer (AST
   is_dangerous) + 구현체 27 개 (telegram, discord, slack, email, notion,
   git, obsidian, calendar, voice, browser-agent, shell-executor, canon-*,
   등) — 전부 재사용.

   **BSVibe 용 트림** (이전이 인터페이스 적합화의 명분):

   - **input/process/output 카테고리 드롭.** 플러그인은 노출하는 무엇이든
     능력으로 가진 Connector — input-only, output-only, action-only, 또는
     혼합.
   - **same-channel `@execute.notify` 가정 드롭.** Inbound 와 outbound 는
     디커플 — Telegram 트리거가 GitHub + Notion + email 로 동시 전송 가능.
   - **신규 capability 데코레이터 모델** (플러그인 객체 1 개, capability 는
     데코레이터로 부착):
     ```
     p = plugin(name="github", credentials=[...], data_jurisdiction="us")

     @p.inbound(trigger={"type": "webhook"})
     async def on_webhook(context, payload) -> TriggerEvent | None: ...

     @p.outbound(artifact_types=["code", "pr"])
     async def deliver_pr(context, event) -> DeliveryResult: ...

     @p.outbound(artifact_types=["issue_comment"])
     async def deliver_comment(context, event) -> DeliveryResult: ...

     @p.action(name="open_pr", mcp_exposed=True)
     async def open_pr(context, branch, title, body) -> dict: ...

     @p.setup
     async def setup(cred_store): ...
     ```
   - **artifact_type 기반 Outbound 라우팅**, 채널 아님 — Delivery Gateway
     가 각 Deliver 이벤트를 토대로 적절한 플러그인의 outbound 를 선택.
   - **`plugin(...)` 에서 선언되는 `data_jurisdiction`** — GDPR
     processing record (§8.4) 의 단일 진실 원천.
   - **`canon-*` 플러그인은 플러그인이 아닌 내부 canonicalization 코드로
     `knowledge/` 로 이동** (vault 관리이지 Connector 관심사가 아님).
   - **`bsnexus-input` 플러그인 폐지** — 이제 모노레포 내부.

   **변경 없이 재사용:**
   - DangerAnalyzer (AST is_dangerous)
   - Credential 스토어 (`.credentials/` JSON) + setup CLI
   - ChatBridge (vault-aware 대화형)
   - PluginLoader / PluginRunner (신규 데코레이터를 위한 약간의 리팩터)
   - 트리거 타입 (cron/webhook/on_input/write_event/on_demand) + outbound
     측 트리거용 신규 `on_deliver`

   **기존 플러그인 구현체 27 개:** 신규 형태로 기계적 리팩터. 대부분 1:1
   — 예: `telegram-input` (input + notify) → `@p.inbound` + `@p.outbound`
   를 갖는 `telegram` 플러그인. 분할 쌍 플러그인 (`email-input` +
   `email-sender`, `git-input` + `git-output`) 은 두 능력을 모두 가진 단일
   connector 로 병합.

   **Intake 모듈은 얇음:** 웹훅 라우터 →
   `plugins.get(name).inbound(payload)` 호출; TriggerEvent 스키마로 검증;
   `trigger:in` 으로 방출. 비-플러그인 트리거 (Direct/Schedule/Decision
   resolution) 는 `backend/intake/` 내부에서 직접 처리.

5. ~~Skill 실행 배치.~~ **잠금 — Claude/OpenClaw 스타일만.**

   Skill = agent-loop 의 **런타임 호출 가능 동작 수정자**, 독립 파이프라인
   아님. (a)-vs-(b) 프레이밍은 Claude/OpenClaw 컨벤션과 동기화하고 트리거
   레이어가 이미 스케줄/이벤트 기반 호출을 처리한다는 점을 인식하며
   해소됨.

   **Skill frontmatter — Claude 형태로 축소:**
   - 필수: `name · version · description` (description = LLM 의 호출 매칭
     신호 — 풍부하게 작성)
   - 선택: `author · allowed_tools · model`
   - Body: skill 의 Markdown 시스템 프롬프트

   **BSage Skill 포맷에서 드롭** (통합 워크플로우에서 이제 중복):
   - `category` — input/process/output 프레임워크 폐기 (플러그인 결정)
   - `trigger` (cron/on_input/on_demand/write_event/webhook) — 트리거
     레이어 (Plugin inbound, Schedule, Direct, Decision resolution) 가
     이미 호출을 처리
   - `read_context` — verify-declaration / plan 시점의 BSage 가중 검색이
     이미 GATHER 수행
   - `output_target` / `output_format` — agent loop 의 Deliver 이벤트
     방출이 APPLY 수행
   - `credentials` — Skill 은 외부 시스템 호출 안 함 (LLM + Vault 만);
     Plugin 영역

   **호출 흐름:**
   ```
   agent loop 실행 중
     ├ work LLM 이 툴 셋에 skill 카탈로그 보유 (name + description)
     ├ LLM 이 현재 스텝의 의도를 skill 의 description 에 매칭
     ├ invoke_skill(name, input) 툴 호출
     ├ runner 가 skill body 를 서브 스텝 시스템 프롬프트로 주입하고,
     │   BSage 검색으로 컨텍스트 프라이밍, 선택적으로 allowed_tools
     │   기준으로 툴 게이트, LLM 호출
     └ 결과가 agent loop 의 다음 plan / Deliver 이벤트로 공급
   ```

   **Frame 의 역할:** Trigger 이벤트가 `suggested_skill` 힌트와 함께 도착하면
   (생산 소스가 설정 — 예: 스케줄 트리거가 "run weekly-digest" 라고 함),
   Frame 이 첫 호출을 위한 힌트로 agent loop 에 전달. 그 외엔 Frame 이
   skill 을 건드리지 않음.

   **BSage SkillRunner 재사용:**
   - SkillLoader / SkillMeta / `_split_frontmatter` — 재사용, frontmatter
     필드 셋 축소
   - LLM 호출 + body 주입 로직 — `invoke_skill` 툴 내부에서 재사용
   - GATHER (`_gather_vault_context`) — BSage 의 일반 검색 API 로 통합
     (단일 소스)
   - APPLY (`_apply_output`) — 폐기 (Deliver 방출이 대체)
   - 독립 파이프라인 모드 — 폐기

   **스케줄 / 이벤트 기반 케이스** (skill 이 트리거를 가졌던 옛 이유):
   별도의 Schedule 엔티티 (또는 `schedule` Plugin) 가 `suggested_skill` 과
   `input` 을 가진 TriggerEvent 발화. 정상 워크플로우 통과. Skill 메타데이터
   자체는 트리거-불가지론.

   **위치:** `backend/skills/` (최상위, `plugins/` 와 병렬).

6. ~~프론트엔드 4 개 → 1 개 컷오버 경로.~~ **단순화** — §7 참조. 단계:
   먼저 role-code 추출, 그 다음 신규 모노레포 가동, 데이터 마이그레이션,
   그리고 레거시 서비스 4 개 + auth.bsvibe.dev 폐지.

### [parked]
- 상세 BSGateway 멀티 계정 라우팅 규칙 (v1 은 기본 휴리스틱으로 충분;
  per-account 예산 정책은 나중).
- 벡터 스토어 선택 (기존 BSage 것 — 유지).
- audit 로그 이상의 LLM 가관측성 (Phase 4+).
- **Supabase 를 대체할 사내 IdP.** 염두에 둠, 연기. §7 통합이 Supabase 를
  `backend/auth/` 뒤로 격리하므로 IdP 교체가 아키텍처 전반 변경이 아닌
  모듈 내부 변경이 됨. 재개 트리거: 가격 변경, 데이터 거주성 요구사항,
  또는 Supabase 가 막는 기능 (커스텀 OAuth 플로우, per-tenant 정책).

---

## 7. 마이그레이션 페이즈

기존 prod 는 제품 백엔드 4 개 + 프론트엔드 4 개 + auth.bsvibe.dev + 공유
라이브러리 8 개 가동 중. 2 개 배포 단위로의 축소는 5 페이즈로 진행.

| 페이즈 | 목표 | 메커닉 |
|---|---|---|
| **0 · 모노레포 스켈레톤** | `bsvibe-app` 레포 생성 | 빈 backend + apps/pwa + apps/site 스켈레톤. CI 그린. |
| **1 · 역할 코드 추출** | 각 엔진의 역할 코드를 `backend/{execution,knowledge,gateway,supervisor}/` 로 복사 | 순수 Python 이전; 이전 중 각 제품의 auth/tenant/REST 래퍼 드롭. 테스트는 코드를 따라감. 기존 서비스 4 개는 변경 없이 계속 실행. |
| **2 · 신규 표면 가동** | `app.bsvibe.dev` 가 신규 PWA + API 서빙 | `/api/auth/*` 의 단일 제품 auth (Supabase 직접). Orchestrator + 워커 + intake/delivery 온라인. 신규 PWA 가 Brief / Decisions / Inside / Skills / Settings 렌더. 프로덕션 트래픽은 여전히 레거시 서비스 4 개. |
| **3 · 데이터 마이그레이션** | 단일 Postgres / Vault / Vector / Redis | 레거시 PG 4 개에서 통합된 것으로 백필. 패리티 후 레거시 PG read-only 전환. |
| **4 · 컷오버** | Trigger 흐름이 `app.bsvibe.dev` 경유; 레거시 폐지 | DNS / 프록시 전환. 레거시 프론트엔드 4 개와 `auth.bsvibe.dev` deprecate 및 종료. 퍼블리시된 공유 라이브러리 (`bsvibe-authz` 등) deprecate; 신규 버전 없음. |
| **5 · 정리** | 레거시 아티팩트 아카이브 | 옛 레포 read-only 아카이브. 크로스 제품 SSO / 라이브러리 캐스케이드 함정의 기억은 역사화. |

확정해야 할 중요 설계 디테일 (다음 세션, 순서대로):

1. ~~횡단 이벤트 스키마.~~ **잠금 — §3.1.**
2. ~~Direct-output compensation 핸들러.~~ **잠금 — §9.**
3. ~~Workspace 부트스트랩.~~ **잠금 — §10.**
4. ~~Per-Run 시퀀스 다이어그램.~~ **잠금 — §11.**
5. ~~마이그레이션 페이즈 0.~~ **잠금 — §12.**

**모든 사전 코드 설계 항목 잠금.** §12 에 따라 Phase 0 에서 구현 시작 가능.
3. **Workspace 부트스트랩.** 신규 파운더 가입 시: 무엇이 생성되는지, 무엇이
   비어 있는지, 첫 Connector + 첫 Direction 으로 어떻게 온보딩 유도하는지.
4. 두 가지 일반 경로에 대한 **Per-Run 시퀀스 다이어그램** — Direct →
   Agent loop → Deliver+Settle, 그리고 Connector inbound → Safe Mode →
   approve → Deliver. 코딩 시작 전에 유용.
5. **마이그레이션 페이즈 0** — 구체적 모노레포 스켈레톤: 디렉터리 레이아웃,
   `pyproject.toml`, 베이스 CI, BSNexus 에서 첫 역할 코드 모듈 이전.

---

## 8. GDPR & 데이터 거버넌스

EU GDPR (그리고 증가하는 유사 법령 목록 — Brazil LGPD, California CCPA,
Korea PIPA, China PIPL) 은 3 레이어로 나뉨. Day-1 아키텍처는 L1 을
무조건 만족해야 함; L2/L3 는 아키텍처상 준비, 인프라는 연기.

### 8.1 L1 — Day 1 무조건 (지금 구축)

| 관심사 | 구현 |
|---|---|
| **삭제권** (Art. 17) | `DELETE /api/workspace/<id>` 캐스케이드: Direction, Decision, Run, Deliverable, BSage 노드, Vault FS 디렉터리, Vector 파티션, audit (익명화). Soft delete + 30 일 hard delete. |
| **접근 / 이동권** (Art. 15/20) | `GET /api/workspace/<id>/export` 가 전체 아카이브 반환 (JSON + markdown 번들, BSage 온톨로지 포함). |
| **처리 기록** (Art. 30) | `GET /api/workspace/<id>/processing-record` 가 어떤 sub-processor 가 어떤 클래스의 데이터를 받았는지 나열. audit 로그 + ModelAccount.data_jurisdiction 에서 생성. |
| **합법적 기반** | `Workspace.legal_basis` (contract / consent). 기본 `contract`. |
| **Sub-processor 공시** | Settings · Privacy 페이지가 DPA 링크와 함께 sub-processor 나열. |
| **저장 시 암호화** | Postgres (관리형 기본), Object store (서버 측 암호화), Vault FS (호스트 디스크 레벨 암호화). |
| **전송 시 암호화** | 모든 곳 TLS. |
| **시크릿 처리** | ConnectorAccount credentials + ModelAccount API 키: KMS 암호화. DB 에 평문 없음. |
| **PII 태깅** | `data/schema/pii.py` 가 PII 포함 컬럼 표시. export/erasure 가 사용. |
| **유출 통지 준비성** | 72 시간 사고 대응 계획 문서화; audit 로그 불변. |
| **Sub-processor DPA** | Supabase, Anthropic, OpenAI, opencode subscription — 출시 전 DPA 서명. 로컬 Ollama = sub-processor 아님 (자체 호스팅). |

### 8.2 L2 — 멀티 리전 / 데이터 거주성 (아키텍처상 준비, 인프라 연기)

일부 EU 고객 (공공 부문, 헬스케어, 뱅킹, 보수적 SMB) 은 EU 내 물리적
데이터를 요구. 현재 연기 (한국의 Mac mini); 나중에 리전 추가가 재작성이
아닌 기계적 작업이 되도록 아키텍처가 준비됨.

| 레이어 | Day-1 준비 |
|---|---|
| `Workspace.region` 필드 | ✓ Day 1. 현재 단일 값 (`self-hosted-kr`). |
| 데이터 레이어 추상화: `db_for(region)`, `vault_for(region)`, `vector_for(region)` | ✓ Day 1. 현재 모두 단일 인스턴스 반환. |
| 리전 인식 Postgres 연결 라우팅 | ✓ Day 1 (추상화 스텁). 두 번째 리전 존재 시 실제 라우터. |
| 리전 인식 Supabase (리전별 프로젝트) | 연기 — EU 고객 도착 시 필요. (주의: parked 사내 IdP 결정과 관련.) |
| 리전 태그된 audit 로그 | ✓ Day 1. |
| Workspace 크로스 리전 마이그레이션 툴 | 연기 — 두 번째 리전 도입 시 작성. |
| 가입 시 리전 선택 | UI 연기; 필드는 DB 에 존재. |

### 8.3 L3 — 엔터프라이즈 데이터 정책 컨트롤 (연기)

규제 산업 B2B (뱅킹, 헬스케어 등) 용. 파운더-as-admin 이 개별 사용자가
더 넓은 관할권으로 계정을 등록하더라도 정책을 *강제* 가능.

| 컨트롤 | 상태 |
|---|---|
| `Workspace.allowed_jurisdictions` (ModelAccount 관할권 화이트리스트) | 연기. 구현 = BSGateway 라우팅 필터. |
| Connector 타깃 화이트리스트 (workspace 별) | 연기. |
| BSage knowledge export 차단 (클라우드 LLM 이 데이터 보는 것 방지 시나리오용) | 연기. |

**주의:** 이전에 제안된 "cloud_llm_allowed" 토글은 드롭. 파운더는 이미
*어떤 ModelAccount 를 등록하는지* 로 workspace 가 사용할 provider 를 제어
(Settings · Models · Add). L3 admin-enforce 토글은 workspace 가 권한이
다른 여러 사용자를 가진 경우에만 의미 — B2B 전까지 범위 밖.

### 8.4 ModelAccount 관할권 — worker SDK 가 선언

추측이나 사용자 입력 의존을 피하기 위해:

- 각 ModelAccount provider 구현 (worker SDK 모듈, 예:
  `backend/gateway/providers/anthropic.py`, `…/openai.py`, `…/ollama.py`,
  `…/azure_openai.py`) 가 SDK 메타데이터로 자신의 **`data_jurisdiction`
  을 선언**.
- 등록 시점에 값이 `ModelAccount` 행으로 복사.
- 단일 진실 원천: SDK 작성자. Audit 친화. Drift 없음.
- 예시:
  - `anthropic` → `"us"`
  - `openai` → `"us"`
  - `azure_openai_eu` → `"eu"`
  - `bedrock_eu` → `"eu"`
  - `ollama` (로컬) → `"self-hosted"`
  - `opencode_subscription` → 그들의 SDK 가 선언 (TBD; DPA 확인)

이것이 processing-record API 에 직접 공급: 모든 LLM 호출이 사용된
ModelAccount + 선언된 관할권을 기록. GDPR Art. 30 이 부수 효과로 만족.

---

## 9. Direct-output compensation

agent loop 이 **Direct 모드** (Resource 의 `output_mode = direct`) 로
방출하면 아티팩트가 외부 시스템에 즉시 도달. Safe Mode 가 대부분 케이스를
커버하지만 Direct 는 파운더가 명시적으로 신뢰하는 케이스를 위해 존재 (특정
레포 브랜치로 auto-merge 등). Direct 아티팩트는 이전 방출을 취소하는
재프레이밍된 Run 이 깨끗하게 undo — 또는 undo 불가한 것을 인정 — 할 수
있도록 정의된 **compensation 경로** 가 필요.

### 9.1 4 단계 분류

| 단계 | 의미 | 예시 |
|---|---|---|
| **T1 — Clean** | 일어난 적 없는 것처럼 복원, 관찰 가능한 흔적 없음 | Notion 페이지 버전 복원 · 메시지 삭제 (편집 윈도우 내) |
| **T2 — With trail** | Undo 가능, 기록/관찰 가능 흔적 남김 | PR close (커밋은 브랜치 히스토리에 잔존) · 메시지 삭제 (편집 마커 보임) |
| **T3 — New artifact** | 원본 취소 불가; compensation 은 *새로운* 보상 아티팩트 | 철회 이메일 · "이전 메시지 무시" 코멘트 · 공개 정정 공지 |
| **T4 — Irreversible** | 외부 부수 효과가 이미 진행 중; 할 수 있는 것 없음 | 결제 처리됨 · 3rd-party 웹훅의 다운스트림 액션 · 이메일 발송 + 읽음 + 행동됨 |

### 9.2 Plugin 계약

각 플러그인은 outbound 등록 시 artifact_type 별 compensation 단계를
선언하고, T1–T3 단계에 대해 `@p.outbound` 와 `@p.compensate` 를 쌍으로 둠.

```python
@p.outbound(
    artifact_types=["pr"],
    compensation_tier="t2_trail",
    compensation_supported=True,
)
async def deliver_pr(context, event) -> DeliveryResult: ...

@p.compensate(artifact_types=["pr"])
async def revert_pr(context, handle: dict) -> CompensationResult:
    """PR close — commits remain on branch (T2)."""
    ...

@p.outbound(
    artifact_types=["webhook_callout"],
    compensation_tier="t4_irreversible",
    compensation_supported=False,
)
async def call_webhook(context, event) -> DeliveryResult: ...
# No @p.compensate — plugin declines responsibility.
```

### 9.3 CompensationResult 스키마

```python
class CompensationResult(BaseModel):
    # Identity
    delivery_result_id: UUID            # which delivery is being compensated
    workspace_id: UUID
    run_id: UUID                         # Run requesting compensation (may differ
                                         # from the original delivery Run)

    # Outcome
    status: Literal[
        "compensated",                   # cleanly reversed (T1)
        "partially_compensated",         # leaves trail or new artifact (T2/T3)
        "compensation_failed",
        "not_supported",                 # plugin declined (T4)
    ]
    tier: Literal["t1_clean", "t2_trail",
                  "t3_new_artifact", "t4_irreversible"]

    # Founder-readable result
    summary: str
    new_external_ref: str | None         # T3: id of the compensating artifact

    # Failure
    error_kind: str | None
    error_message: str | None
    retryable: bool = False

    # Audit
    compensated_at: datetime
    plugin: str
```

### 9.4 DeliveryResult 확장

§3.1 `DeliveryResult` 에 compensation 용 3 개 필드 추가:

```python
compensation_tier: Literal["t1_clean", "t2_trail",
                           "t3_new_artifact", "t4_irreversible"]
compensation_supported: bool             # plugin has @p.compensate handler
compensates_delivery_id: UUID | None     # link to the DeliveryResult this
                                         # delivery is itself a compensation of
                                         # (T3 chain — usually None)
```

이로써 파운더 UI 는 단계별로 사전 판정된 retract 어포던스를 표시할 수
있고, 다운스트림 툴링은 compensation 체인을 추적할 수 있음.

### 9.5 호출 규칙

- **결코 자동이 아님.** Compensation 은 항상 파운더가 개시:
  - 전달된 아티팩트에 명시적 "Retract"
  - 재프레이밍된 Run: OS 가 이전 Direct 방출 나열, 파운더가 각
    compensation 을 개별 승인
  - 부분 Direct 방출이 있는 실패 Run: 동일 — 명시적 확인
- **Safe Mode 큐 항목은 compensation 불필요** — workspace 를 떠난 적
  없음; 재프레이밍은 단순 dequeue.
- **멱등성 필수.** `compensate(handle)` 은 이미 적용됐다면 no-op 이어야
  함. 재호출은 조용히 성공 반환 필수. (네트워크 재시도 이중 액션 방지.)
- **실패는 Decision 유사 수동 처리 프롬프트로 표면화.** 공격적 자동 재시도
  없음; OS 가 파운더에게 확인하고 진행하도록 요청.
- **Compensation 체인:** T3 가 생산한 새 아티팩트는 기본적으로
  `compensation_supported=false` (철회의 보상 없음). 정말 의미 있으면
  플러그인이 오버라이드 가능.
- **번들 compensation (`bundle_id` 경유):** 최선 노력. 번들 반복, 각각
  자체 단계에 따라 보상; 번들 내 T4 항목은 "retract 불가" 로 표면화되지만
  나머지를 블록하지 않음.

### 9.6 UX 접점 (백엔드 잠금 범위 밖, 여기 기록)

- Run 뷰의 전달된 아티팩트는 `compensation_supported=true` 일 때만
  "Retract" 어포던스 표시. T4 는 툴팁과 함께 비활성화.
- 단계 라벨 가시: `T1 (clean)` / `T2 (leaves trail)` / `T3 (sends a
  follow-up)` / `T4 (cannot undo)`.
- Direct 모드 사전 경고: 파운더가 Resource 에 Direct 활성화하려 하고
  artifact_type 이 T3/T4 면 *"이 전달은 깨끗하게 undo 할 수 없음"* 표시 —
  Safe Mode 권장.

---

## 10. Workspace 부트스트랩

파운더 가입 시 생성되는 것, 기본 상태, 온보딩 흐름, 그리고 workspace 가
오래된 데이터를 누적하지 않도록 하는 보존 정책.

### 10.1 가입 시 생성되는 엔티티

```
Supabase OAuth callback
  ├── User row              (email, supabase_user_id, created_at)
  ├── Workspace row         (id, region='self-hosted-kr',
  │                          legal_basis='contract', created_at)
  └── Membership row        (user_id, workspace_id, role='owner',
                             joined_at=now)

Vault FS:
  vault/<workspace_id>/
    ├── garden/             (empty)
    ├── seeds/              (empty)
    └── actions/            (empty)

BSage knowledge graph:
  per-workspace partition created, 0 nodes
```

나머지 모두는 빈 상태로 시작: Product 없음, ConnectorAccount 없음,
ModelAccount 없음, Skill 없음, Resource 없음, Direction/Run/Decision
히스토리 없음.

### 10.2 기본값

| 정책 | 기본값 |
|---|---|
| Resource `output_mode` (신규 바인딩) | `safe` (Safe Mode 큐) |
| Workspace `region` | `self-hosted-kr` |
| Workspace `legal_basis` | `contract` |
| Cloud LLM 허용 | true (L3 엔터프라이즈 강제 연기) |
| Safe Mode 큐 보존 | **90 일 active → 30 일 archived → hard delete** (§10.5) |
| Soft delete 윈도우 (workspace) | 30 일 |

### 10.3 온보딩 상태 머신

4-step 흐름; 1 단계만 필수.

```
[fresh signup] → Welcome (one screen, "let's get BSVibe ready")
                    │
                    ▼
1. Connect a model           (REQUIRED)
   · provider picker (opencode recommended; Anthropic / OpenAI / Local Ollama)
   · plugin.setup() runs — OAuth or key input
   · ModelAccount created
                    │
                    ▼
2. Name your first Product   (skippable)
   · name + one-line description
   · Product created with empty Resource list
                    │
                    ▼
3. Connect a source          (skippable)
   · 4–5 recommended Connectors visible (GitHub · Notion · Email · Slack · Drive)
   · plugin.setup() runs → ConnectorAccount; if step 2 done, auto-binds a
     Resource to that Product
                    │
                    ▼
4. Try a Direction           (skippable)
   · prefilled suggestion based on what's been set up
   · or "Skip — take me to Brief"
                    │
                    ▼
[Brief home]
```

1 단계 게이트: ModelAccount 가 존재할 때까지 Brief 표면은 *"작업 시작하려면
모델을 연결하세요"* 모달로 차단. 2/3/4 단계는 비차단 — 파운더는 모델만
연결된 채 Brief 에 머물 수 있음.

### 10.4 빈 상태 카피 (표면별)

| 표면 | 시점 | 카피 / CTA |
|---|---|---|
| Brief | 0 Products | "아직 진행 중인 것이 없습니다. 아래 `+` Direct 로 시작하세요." |
| Brief | Products 존재, 0 active Runs | "Products 가 설정되었습니다; 진행 중인 작업 없음. Direct 를 시도하세요." |
| Decisions | 0 pending | "지금 당장 판단이 필요한 것 없습니다." (양호 상태) |
| Inside (그래프) | 0 nodes | "BSVibe 가 아직 당신을 모릅니다. Direct 와 Decide 를 거치며 그래프가 자랍니다." |
| Settings · Connectors | 0 | "작업이 들어오고 나갈 수 있도록 외부 시스템을 연결하세요. 일반: GitHub · Notion · Email" |
| Settings · Models | 0 | (도달 불가 — 1 단계 게이트) |
| Skills · library | 0 설치 | "Skill 은 재사용 가능한 동작입니다. 갤러리를 둘러보거나 직접 작성하세요." |
| Product 페이지 | 비어있음 | "이 Product 에 아직 작업 없음. + Direct 또는 Resource 링크." |

### 10.5 보존 — Safe Mode 큐 TTL

무기한 Safe Mode 큐는 GDPR Art. 5(1)(e) (저장 제한) 를 위반하고 오래된
credential 바인딩 외부 컨텍스트를 누적. 경계된 정책:

| 일 | 상태 | 파운더 경험 |
|---|---|---|
| 0 | `pending` | 큐의 항목, 액션 대기 |
| 60 (= expires_at − 30d) | 리마인더 발화 | 인앱 + 연결된 알림 채널: "47 항목이 30 일 후 만료 — 큐 검토" |
| 90 (= expires_at) | 자동 `archived_expired` | 활성 큐에서 제거; Archived 에서 여전히 조회 가능; **외부로 전달 안 됨** |
| 120 (= archived + 30d) | hard `deleted` | 행 제거; audit 로그가 존재 기록 유지 (workspace 삭제 시 익명화) |

**연장:** 파운더는 만료 임박 항목에 *"+30 일"* 클릭 가능. 항목당 **최대
2 회 연장**, 총 활성 수명 150 일 캡. 그 이상은 항목에 행동하거나 만료시켜야
함.

**일괄 자동 만료 어포던스 없음.** 항목은 자체 시계로 만료. 파운더 액션은
항상 항목별 또는 번들별.

**왜 archive 후 delete:** archive 는 파운더가 회고를 위해 "지난 분기 무엇이
큐에 있었나?" 질문할 수 있게 함. Hard delete 는 저장 제한을 강제.

### 10.6 팀으로의 경로 (연기 — 스키마는 지금 지원)

팀 workspace 출시 시 (v1 후):

| 능력 | 변경 사항 |
|---|---|
| Membership 테이블 | 이미 존재. 비-owner 역할용 invite/accept 행 추가. |
| 초대 흐름 | Settings · Members 의 신규 API + UI |
| 권한 | BFF 가 Membership 행에서 `current_role` 해결, 역할별 게이트 |
| 사용자당 다중 workspace | 상단 내비 workspace 전환기; Membership 이 N 개 workspace 로 팬아웃 |
| Workspace 별 청구 | 좌석별 또는 workspace 별 (그때 결정) |
| Audit 로그 입도 | workspace 내 사용자별 귀속 |

이 중 어느 것도 v1 스키마 마이그레이션이 필요 없음 — 기존
Membership/role/workspace_id 트리플에 대한 신규 코드 경로만.

### 10.7 Workspace 삭제 (§8.1 상호 참조)

`DELETE /api/workspace/<id>` (owner 전용):
- Soft delete (`deleted_at = now`) — workspace 숨김, 모든 멤버가 액세스
  상실. 30 일 동안 복구 가능.
- 30 일 후: hard delete 캐스케이드 — 모든 행 (Products, Resources,
  Directions, Runs, Decisions, Connectors, Models, Skills, Membership),
  vault FS 디렉터리, vector 파티션, Safe Mode 큐 항목 제거. Audit 로그
  엔트리 익명화 (Art. 30 기록용 보존).

### 10.8 Workspace 비활성 (연기)

큐 TTL 과는 다른 관심사. 오늘: workspace 는 활동과 무관하게 owner 가
존재하는 한 지속. 나중에 실제 데이터 기반 정책: N 개월 휴면 시 연간
재확인, 2 년 무활동 시 hard delete 등. 사용 데이터가 임계값을 알릴
때까지 연기.

---

## 11. Per-Run 시퀀스 다이어그램

다이어그램 3 개: 가장 흔한 엔드투엔드 경로 2 개 (Direct, Connector inbound
+ Safe Mode), 그리고 agent-loop 반복 한 번의 줌인.

### 11.1 Direct 경로 — 파운더가 개시, Direct-모드 출력

파운더가 PWA Direct 작성에 타이핑; agent loop 실행; 전달이 곧장 외부로
나감 (Resource `output_mode = direct`).

```mermaid
sequenceDiagram
    actor Founder
    participant PWA
    participant BFF
    participant Intake
    participant R as Redis Streams
    participant Orch as Orchestrator
    participant Frame as Frame worker
    participant BSage
    participant Loop as Agent-loop worker
    participant Gw as BSGateway
    participant Sup as Supervisor
    participant Disp as Delivery dispatcher
    participant Plugin
    participant Ext as External system

    Founder->>PWA: Direct compose
    PWA->>BFF: POST /api/direct {intent_text, product_id?}
    BFF->>Intake: direct handler (build TriggerEvent)
    Intake->>Intake: schema validate
    Intake->>R: XADD trigger:in
    R-->>Orch: consume
    Orch->>Frame: dispatch (TriggerEvent)

    Frame->>BSage: retrieve (intent + workspace context)
    BSage-->>Frame: primed nodes
    Frame->>Gw: LLM cheap (interpret + classify + skill match?)
    Gw-->>Frame: framing
    Frame->>R: XADD direction:framed

    R-->>Orch: consume
    Orch->>Loop: dispatch (Direction)

    loop until terminal
        Loop->>Loop: plan/act/verify (see §11.3)
        Loop->>R: XADD deliver:event (mid-loop)
        Loop->>R: XADD settle:event (mid-loop)
    end

    par Delivery fan-out
        R-->>Disp: consume deliver:event
        Note over Disp: Resource.output_mode = direct
        Disp->>Plugin: outbound(event)
        Plugin->>Ext: external API call
        Ext-->>Plugin: result + external_ref
        Plugin-->>Disp: DeliveryResult{compensation_handle}
        Disp->>PWA: SSE delivered
    and Settle fan-out
        R-->>BSage: consume settle:event
        BSage->>BSage: canonicalize + graph write
        BSage->>PWA: SSE Inside graph update
    end

    Loop->>R: XADD loop:terminal (verified)
    R-->>Orch: consume
    Orch->>PWA: SSE Run terminal state
```

### 11.2 Connector inbound + Safe Mode 경로

외부 GitHub 웹훅 도착; agent loop 실행; 전달이 외부로 나가기 전 파운더
승인을 위해 Safe Mode 큐에 들어감 (Resource `output_mode = safe` —
비-파운더 트리거의 기본).

```mermaid
sequenceDiagram
    actor Ext as GitHub
    participant Intake
    participant Plugin as GitHub plugin
    participant R as Redis Streams
    participant Orch as Orchestrator
    participant Frame as Frame worker
    participant Loop as Agent-loop worker
    participant Disp as Delivery dispatcher
    participant SMQ as Safe Mode queue
    participant PWA
    participant BFF
    actor Founder

    Ext->>Intake: POST /api/webhooks/github (signed payload)
    Intake->>Plugin: github.inbound(payload, headers)
    Plugin->>Plugin: signature verify + parse
    Plugin-->>Intake: TriggerEvent{source=connector_inbound,<br>connector=github, resource_id, idempotency_key}
    Intake->>Intake: schema validate + idempotency check
    Intake->>R: XADD trigger:in

    R-->>Orch: consume
    Note over Orch: Receive — resolve Product via Resource binding,<br>derive suggested_artifact_type from metadata
    Orch->>Frame: dispatch
    Frame->>Frame: LLM cheap — interpret + decide path
    Frame->>R: XADD direction:framed

    R-->>Orch: consume
    Orch->>Loop: dispatch (Direction)

    loop until terminal
        Loop->>Loop: plan/act/verify (see §11.3)
        Loop->>R: XADD deliver:event
    end

    R-->>Disp: consume deliver:event
    Note over Disp: Resource.output_mode = safe<br>(default for non-founder triggers)
    Disp->>SMQ: insert {expires_at = now + 90d, status=pending}
    Disp->>PWA: SSE delivery:safemode_queued

    Founder->>PWA: opens Brief, sees "Needs you" lane
    PWA->>BFF: GET /api/safemode/queue
    BFF-->>PWA: pending items (with compensation_tier shown per item)
    Founder->>PWA: clicks Approve (per item or bundle)
    PWA->>BFF: POST /api/safemode/approve
    BFF->>SMQ: mark approved
    BFF->>Disp: trigger dispatch of approved items

    Disp->>Plugin: github.outbound(deliver_event)
    Plugin->>Ext: open PR / post comment / etc.
    Ext-->>Plugin: external_ref + url
    Plugin-->>Disp: DeliveryResult{compensation_handle}
    Disp->>SMQ: mark delivered
    Disp->>PWA: SSE delivered
```

### 11.3 Agent-loop 반복 — 줌인

한 반복: plan → act → verify-declaration → verify-run → branch
(continue / ask / done). 반복 중간의 AskUserQuestion 은 Decision 이
해소될 때까지 일시정지. Deliver/Settle 이벤트는 사이드 채널로 반복 중간에
방출 (§11.1/11.2 에 이미 표시).

```mermaid
sequenceDiagram
    participant Loop as Agent-loop worker
    participant Gw as BSGateway
    participant BSage
    participant Sandbox as DinD sandbox
    participant Sup as Supervisor
    participant R as Redis Streams
    actor Founder

    Note over Loop: iteration start
    Loop->>Gw: LLM (heavy) — plan next step
    Gw-->>Loop: plan + (optional) AskUserQuestion

    alt LLM emits AskUserQuestion
        Loop->>R: XADD decision:pending
        Note over Loop: loop paused
        Founder->>R: XADD decision:resolved (via PWA → BFF → Orch)
        R-->>Loop: resume with answer
    else normal plan
        Loop->>Sandbox: act — tool calls<br>(file_read/write/edit/shell_exec)
        Sandbox-->>Loop: tool results
        Loop->>R: XADD deliver:event (if partial artifact ready)
        Loop->>R: XADD settle:event (if observation worth capturing)

        Note over Loop: verify-declaration
        Loop->>BSage: retrieve (signals from this step's changes)
        BSage-->>Loop: relevant canonical patterns + context
        Loop->>Loop: assemble verify contract<br>(work-declared + BSage-retrieved)

        Loop->>Sup: run verify in sandbox (assembled contract)
        Sup-->>Loop: verdict + evidence
        Sup->>R: XADD audit (always-on observability)

        alt verify passed AND work complete
            Loop->>R: XADD loop:terminal (verified)
        else verify failed
            Note over Loop: continue iterating — re-plan with verifier output
        else work incomplete
            Note over Loop: continue iterating — next plan step
        end
    end
```

### 11.4 다이어그램에 보이는 핵심 불변

- **Redis Streams 가 어디에나** — 모든 워커 간 핸드오프는 스트림 이벤트;
  워커가 자신의 큐 소비. 워커 타입 간 직접 동기 호출 없음.
- **Intake 에서의 멱등성** — XADD `trigger:in` 전에 `idempotency_key`
  체크 (재전송된 웹훅은 no-op).
- **라이브 UI 용 SSE** — Brief / Decisions / Run 페이지가 구독; 폴링 없음.
- **Delivery 와 외부 사이의 Safe Mode 큐** — `output_mode = safe` 전달은
  파운더 승인까지 플러그인의 `outbound()` 가 *호출되지 않음*. Direct 전달은
  큐를 완전히 건너뜀.
- **AskUserQuestion 은 워커를 해제하지 않고 루프 일시정지** — 워커가
  해결 이벤트 대기. 긴 일시정지가 샌드박스 리소스를 묶지 않음: 샌드박스
  상태가 체크포인트됨; §11.5 참조.
- **verify-declaration 에서의 BSage 검색** — Frame 의 컨텍스트 프라이밍과
  동일한 호출 사이트, 쿼리 입력만 다름. 단일 API.

### 11.5 긴 일시정지 (Decision 주도) — 체크포인트 계약

Decision 이 agent loop 를 일시정지할 때 재개를 위해 샌드박스 상태가 필요할
수 있음. 두 옵션:

- (a) 샌드박스 계속 실행 (비용: 일시정지된 Run 당 메모리 고정)
- (b) 샌드박스를 디스크로 체크포인트, 워커 해제; 재개 시 복원

분 단위로 해결되는 Decision: (a) 로 충분. 시간/일 단위 (예: 파운더 여행)
일 수 있는 Decision: (b) 필수.

**잠금:** 샌드박스 체크포인팅은 agent loop 워커의 일부. 일시정지 상태
**15 분** 후 기본 체크포인트. 상태는 `run/<run_id>/checkpoint/` 하위
object store 에 영속; 복원은 샌드박스 cold-start 와 상태 리플레이 (BSNexus
가 재시도용 유사 패턴 이미 보유).

---

## 12. 마이그레이션 페이즈 0 — 모노레포 스켈레톤

§7 의 Phase 0 구체화: 무엇이 생성되는지, 그 안에 무엇이 있는지, 무엇이
CI 를 통과하는지, 언제 "Phase 0 완료" 가 참인지. 이후 역할 코드 추출
(Phase 1) 이 모듈별로 진행.

### 12.1 저장소

- **GitHub:** `BSVibe/bsvibe-app` (비공개). §2.2 에 따른 단일 백엔드 +
  PWA 모노레포. 기존 `BSVibe/internal-docs`, `BSVibe/bsvibe-site` 와 동일
  GitHub org.
- **Day 0 부터 브랜치 보호:** main 보호 · 필수 PR 리뷰 (1 명; 솔로 파운더
  단계에서 self-approve 허용) · 필수 CI · 선형 히스토리.
- **마케팅 사이트 별도 유지:** 기존 `bsvibe-site` 레포 미변경.

### 12.2 디렉터리 레이아웃 (Phase 0 베이스라인)

```
bsvibe-app/
├─ apps/
│  └─ pwa/                          Next.js scaffold (placeholder pages)
│     ├─ app/                       Next.js app-router layout
│     ├─ package.json
│     └─ tsconfig.json
├─ backend/
│  ├─ api/
│  │  ├─ __init__.py
│  │  ├─ main.py                    FastAPI app entry, includes /api/health
│  │  └─ auth/                      (stub — fills in Phase 1)
│  ├─ orchestrator/                 (stub)
│  ├─ intake/                       (stub)
│  ├─ delivery/                     (stub)
│  ├─ execution/                    (stub — populated by Phase 1)
│  ├─ knowledge/                    (stub)
│  ├─ plugins/                      (stub)
│  ├─ skills/                       (stub)
│  ├─ gateway/                      (stub)
│  ├─ supervisor/                   (stub)
│  ├─ workers/                      (stub)
│  ├─ data/
│  │  ├─ __init__.py
│  │  ├─ models.py                  SQLModel base + Workspace stub
│  │  └─ migrations/                alembic init
│  └─ shared/                       lifted bsvibe-authz + bsvibe-fastapi
│                                   in Phase 0 (smallest first lift)
├─ tools/
│  └─ cli/                          (stub)
├─ tests/
│  ├─ test_health.py                proves the stack
│  └─ test_smoke.py                 proves PG + Redis reachable
├─ deploy/
│  ├─ Dockerfile.backend            FastAPI image
│  ├─ Dockerfile.pwa                Next.js image
│  └─ compose.yaml                  Postgres + Redis + backend + pwa
├─ .github/
│  └─ workflows/
│     ├─ ci.yml                     lint + type + test
│     └─ build.yml                  Docker builds on main
├─ .devcontainer/
│  └─ devcontainer.json             Mac mini consistent dev env
├─ pyproject.toml                   uv-managed; Python 3.11+
├─ uv.lock
├─ ruff.toml
├─ mypy.ini
└─ README.md
```

### 12.3 툴링 베이스라인

- **Python**: 3.11+, `uv` 패키지 매니저 (프로젝트 규칙에 따라),
  `pyproject.toml`.
- **Linter**: `ruff check + ruff format` (프로젝트 규칙에 따라).
- **타입**: `backend/` 에 `mypy --strict`.
- **프론트엔드**: Next.js 15, TypeScript, Biome (또는 eslint+prettier).
- **테스트**: 백엔드에 `pytest --cov=backend --cov-fail-under=80`; PWA 에
  Vitest.
- **Pre-commit**: `pre-commit` 프레임워크로 ruff + mypy + biome.
- **CI** (`.github/workflows/ci.yml`):
  - push / PR 시: ruff check · mypy · cov 게이트 동반 pytest · biome
    lint · pwa typecheck · pwa build
- **로컬 dev**: `docker compose up` 이 PG + Redis + backend + pwa 기동.
- **Devcontainer**: Python 3.11, uv, Node 20, postgres + redis 서비스,
  Mac mini 프로덕션 환경과 동일.

### 12.4 Phase 0 수락 기준

Phase 0 는 다음 *모두* 가 참일 때 완료:

1. 레포 존재; §12.2 에 따른 구조.
2. 빈 모듈로 `main` 에 CI 그린 (lint, type, test 모두 통과).
3. `docker compose up` 이 로컬에서 스택 기동; `curl
   http://localhost:8000/api/health` 가 `200 {"status": "ok",
   "version": "...", "git_sha": "..."}` 반환.
4. **스모크 테스트 통과**: `tests/test_smoke.py` 가 PG `workspaces`
   테이블에 행 작성 (`id`, `created_at` 만), Redis `health:test` 스트림에
   메시지 발행, 소비, 왕복 어서트. PG + Redis 가 스텁이 아닌 실제로 배선됨을
   증명.
5. PWA 가 `localhost:3000/` 에서 플레이스홀더 로그인 페이지 렌더. Auth
   는 여전히 스텁이지만 라우팅 작동.
6. 첫 이전 완료: `backend/shared/` 에 `bsvibe-authz` 및 `bsvibe-fastapi`
   라이브러리 코드 사본 채워짐, 내부 import 로 리팩터. 해당 라이브러리의
   기존 테스트 포팅 후 새 구조에서 통과.
7. `.devcontainer/devcontainer.json` 이 fresh checkout 을 VS Code 에서
   열고 수동 셋업 없이 `uv sync && uv run pytest` 실행 가능하게 함.
8. README 가 문서화: 사전 필요 툴 · `docker compose up` 흐름 · 테스트
   실행 방법 · 기여 방법 (PR + CI 요구사항).

### 12.5 첫 모듈 이전 순서 (Phase 1 미리보기)

Phase 0 후 Phase 1 이 역할 코드 이전. 순서는 *의존성 깊이* 로 선택 (의존성
가장 작은 것 먼저):

1. `shared/` — Phase 0 에서 이미 완료 (auth + fastapi 유틸리티).
2. `gateway/` — LLM 디스패치 (shared + httpx 에만 의존).
3. `plugins/` — 플러그인 로더 + DangerAnalyzer + 데코레이터 (shared 에
   의존; 구현체 27 개는 프레임워크가 오른 후).
4. `knowledge/` — BSage 그래프 + canonicalization + 검색 API (shared +
   플러그인-에서-수집 경로용 plugins 에 의존).
5. `skills/` — Skill 로더 + invoke_skill (shared + gateway 에 의존).
6. `execution/` — BSNexus 역할 코드: 상태 머신 + 샌드박스 + 툴 루프 +
   verifier (gateway + knowledge + supervisor + plugins 에 의존).
7. `supervisor/` — audit 로그 + 샌드박스 스크립트 러너 (shared +
   execution 에 의존).
8. `intake/`, `delivery/`, `orchestrator/`, `workers/` — 모든 것을 묶는
   글루 레이어. 마지막에 구축.

각 Phase-1 이전은 자체 PR; 이전 사이 CI 가 그린 유지; 테스트는 코드를
따라감.

### 12.6 Phase 0 가 명시적으로 포함하지 않는 것

- 완전히 배선된 인증 (스텁만; Phase 1 의 `api/auth/`)
- `shared/` 이전을 넘어선 BSNexus / BSage / BSGateway / BSupervisor 의
  어떤 역할 코드
- 프론트엔드 표면 (플레이스홀더 로그인 페이지만)
- 플러그인 구현체 (로더 스텁만)
- Mac mini 배포 (Phase 2)

Phase 0 는 *서있는 구조*. 요점은: 이 순간부터 모든 Phase-1 이전이 CI 를
안전망으로 갖는 별개의 머지 가능한 PR.
