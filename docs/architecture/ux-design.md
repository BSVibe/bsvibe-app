# BSVibe — UX Design (Pass 1)

> Date: 2026-05-19
> Status: **WORKING — not locked.** 상위 문서:
> `docs/architecture/strategy-synthesis.md` (전략 SoT).
> 이 pass는 founder 경험을 moment 단위로 설계한다. OS workflow
> (synthesis §14)는 이 문서에서 *역으로* 도출되며, 그 반대가 아니다.

---

## 0. 방법 — UX-first

내부 workflow를 먼저 설계하면 UX가 레거시 BSNexus 파이프라인의 종속 변수가 되고,
4-product 통합이 열어준 선택지를 보지 못한다. 따라서: **founder 경험을 먼저
설계하고, 거기서 workflow를 도출한다.**

경험은 founder가 반복적으로 처하는 **moment** 에서 설계한다 — 화면이나
파이프라인이 아니라.

synthesis에서 가져온 hard constraint 2개:

- **관리 콘솔이 되어선 안 된다.** org chart, agent roster, budget dashboard,
  KPI grid 없음. 그게 Paperclip의 제품 전부이고, 그걸 피하는 것이 카테고리
  차별화다 (synthesis §9). glass box는 **작업의 증거 (proof of work)** 로
  열린다 — 관리할 회사로 열리지 않는다.
- **Craft bar = Notion 수준의 calm** — 명료함, 차분함, 노이즈 없는 밀도,
  typography와 whitespace로 잡는 위계, 색은 상태 의미에만 사용.

---

## 1. 경험 모델

### 1.1 Shell — 단일 앱

하나의 로그인, 하나의 앱. 좌측 rail:

- **Brief** — 홈 (= Fleet, L0). 기본 진입 surface.
- **Decisions** — 가로지르는 "needs you" 인박스.
- **Inside** — Run (L2) / Process (L3) 진입; proof + inspection surface.
- **Products** — founder 제품 목록 (어떤 L1으로든 jump).

**Direct** (founder-metaphor 4번째 동사)는 별도 페이지가 아닌 **편재하는
입력 (omnipresent input)** 으로 구현된다 — Brief 위의 박스, 그리고 전역
command bar. 가장 빈번한 행동이므로 어디서든 도달 가능해야 하고, 이동해서
도달하면 안 된다.
*(미정 micro-point: Direct가 순수 command-bar 인지 rail 항목도 겸하는지 —
Direct moment 설계 시 결정.)*

### 1.2 공간 모델 — zoom

`Fleet (L0) → Product (L1) → Run (L2) → Process (L3)`. founder가 zoom in;
한 번 클릭으로 내려가고, breadcrumb으로 올라온다. 4 layer (execution/power/
safety/memory)는 모든 레벨에서 **ambient** — cost, guardrail status, "사전에
알려져 있던 것"이 *문맥 안에서* 나타나고, 자기 자신의 destination을 갖지 않는다.

### 1.3 유동적 입력 — 통합이 열어준 선택지

knowledge (BSage), execution (BSNexus), delivery가 하나의 OS이므로, **단일
입력 박스가 모든 intent를 처리한다.** founder는 "내가 질문을 하는 건가 작업을
시작하는 건가"를 고르지 않는다 — 타이핑하면 OS가 라우팅한다 (synthesis §14
Frame/Route): ontology로 답할 수 있으면 → 답; 작업 형태면 → Run. 이 유동성은
통합이 가능하게 한 UX 결정이다.

### 1.4 신뢰는 느껴진다, 표시되지 않는다

founder는 *효과* 를 본다 — 작업 진행, 같은 실수 반복하지 않음, 유지되는 "done"
— 기계 장치 (budgets, rounds, ratchet 내부)가 아니다. ratchet, decomposition,
verification은 비가시. proof는 보이고, 장치는 보이지 않는다.

---

## 2. 6 founder moment — 설계 scaffold

| # | Moment | founder가 필요로 하는 것 | 주 surface |
|---|---|---|---|
| 1 | **Direct** | 아이디어/지시를 최소 마찰로 던짐 | Direct input / command bar |
| 2 | **Passive trigger** | 자리 비운 사이 email / issue가 작업을 깨움 — 복귀하며 확인 | Brief (Fleet) |
| 3 | **Glance** | "전부 뭘 하고 있나? 나를 필요로 하는 것은?" | Brief (Fleet) — 아래 §3 |
| 4 | **Decide** | taste/판단이 필요한 몇 건을 결정 | Decisions → Run 내부로 |
| 5 | **Review** | "정말 끝난 건가? 믿을 수 있나?" — proof | Run proof surface (L2) |
| 6 | **Inside** | "왜 그렇게 했지 / BSVibe가 나에 대해 뭘 아나?" + ontology 교정 | Inside (L2/L3) + ontology view |

이 pass는 **moment 3 (Glance)** 를 깊이 설계한다 — 홈이고, founder 원래 통증
("한 번에 여러 개 관리")의 직접적 답. moment 1, 2, 4, 5, 6은 후속 pass에서
설계한다.

---

## 3. Moment 3 — The Glance (Brief / Fleet, L0)

### 3.1 Moment

founder가 BSVibe를 연다 — 혹은 자리 비웠다 돌아온다. ~5초 안에 다음 두 질문에
이 우선순위로 답해야 한다:

1. **나를 필요로 하는 것은?** (실제로 founder를 요구하는 유일한 것)
2. **나머지는 괜찮은가?** (안심)

그리고 무엇이든 한 번 클릭으로 진입할 수 있어야 한다. 이 화면의 일은 그게
전부다. 나머지는 절제.

### 3.2 설계 원칙

1. **Decisions 먼저.** founder를 *필요로 하는* 단 하나는 결정이다 — 상단의
   슬림한 strip에 자리한다. 비어 있으면 → calm, quiet 상태.
2. **Products는 metric 카드가 아니라 차분한 status lane.** 각 제품은 하나의
   lane: 이름 + **plain-language status line** + verdict 힌트. status line은
   *서술* 된다 ("writing tests for the related-posts feature · 4m in"), 절대
   "Run #4 · 6 rounds · $0.04" 식이 아니다. 기계는 비가시 유지 (§1.4).
3. **Recently shipped = 조용한 안심**, 각 항목마다 proof verdict 동반.
4. **단일 Direct input**, 유동적 (§1.3).
5. **실시간 감시 없음.** 이는 glance이지 monitoring console이 아니다 (UX
   anti-goal). 정지 상태에서 calm.

### 3.3 Product lane 상태

| Glyph | 상태 | Status line 표시 |
|---|---|---|
| ● (neutral) | **working** | 무엇을 하는지 plain language로 + 경과 시간 |
| ● (amber) | **needs you** | 질문 한 줄 (상단 strip에도 동일 표시) |
| ↑ (neutral) | **just triggered** | "started from a GitHub issue / email" — decomposing… (moment 2가 여기로 안착) |
| ✓ (green) | **shipped** | 무엇이 shipped 됐는지 → PR link · verdict |
| ○ (faint) | **idle** | — |

색은 *needs you* (amber)와 *verified/shipped* (green)에만 등장. working과
idle은 neutral. lane 한 번 클릭 → 그 Product (L1); "needs you" 행 한 번
클릭 → 그 Run 안의 Decision으로 직행.

### 3.4 Mockup

Stitch high-fidelity, Notion craft bar:
- Project `projects/5698840467483031448` ("BSVibe — UX Moments")
- Screen `23c8e30bdedd472eb18d48c62c8a2a8c` — Brief / Fleet home.
- `mcp__stitch__fetch_screen_image` 로 재조회.

레이아웃: 좌측 rail (BSVibe wordmark · Brief/Decisions/Inside · Products
list · account) + 중앙 ~760px 컬럼 — 상단 "Needs you" amber strip → Direct
input → "Your products" status lane → "Recently shipped".

### 3.5 왜 이게 대시보드가 아닌가

Paperclip/admin 대시보드라면 다음을 보여줬을 것이다: 온라인 agent, budget
burn, token 차트, org chart, throughput KPI. 이 화면은 **그 어느 것도 보여주지
않는다.** 제품을 *사람이 말로 설명하듯이* 보여준다 — "bsvibe-site is writing
tests; nexus-core needs a call from you; data-pipeline shipped." 이건
**plain language로 기술된 status ledger**, Notion 홈 페이지의 calm —
operations console이 아니다. 그 절제 *자체* 가 차별화다.

### 3.6 다른 moment와의 연결

- **Moment 2 (passive trigger):** founder가 자리 비운 사이 발화된 trigger는
  여기 `↑ just triggered` lane으로 표면화 — founder가 돌아와 작업이 이미
  움직이고 있는 걸 보고, 출처가 명명되어 있다.
- **Moment 4 (Decide):** 상단 "Needs you" strip이 진입점; 행 → 그 Run 안의
  Decision으로.
- **Moment 5 (Review):** `✓ shipped` lane / Recently shipped 행 → Run proof
  surface (L2).

---

## 4. IA 결정 lock (이번 라운드)

레거시 v1 mockup 이후 founder가 IA를 교정. 아래 lock은 충돌 시 §1.1/§1.2 를
대체한다.

- **Nav = Brief · Decisions · Inside · (Products list) · Settings.** "Home"
  으로 개명 없음 — Brief 유지. Direct는 **화면이 아님** — 전역 액션
  (⌘K + FAB), 따라서 레거시 "M1 Direct" mockup은 폐기 (Brief의 부분집합이었음).
- **Zoom:** `Brief (L0, cross-product Glance) → Product (L1, per-product home)
  → Run (L2, Delivery Report) → Process (L3)`. **Inside는 자기 축** —
  Run으로의 zoom drilldown이 아니다. Run은 **Product 아래** 에 산다
  (breadcrumb `Products / <product> / Runs`), Inside 아래가 아니다.
- **Inside = BSage graph view.** Obsidian 스타일 knowledge graph, 카테고리별
  색 코딩 (Conventions / Preferences / **Guards** / Domain), 노드 상세는
  우측 패널 (desktop) / 하단 시트 (mobile). flat list는 BSage를 평탄화한 것에
  불과했음.
- **Product는 source-agnostic.** Product는 *founder가 만드는 것* — 여러
  Connector를 바인딩 가능 (GitHub repo + Notion wiki + Vercel preview +
  Email + …). 소스 바인딩은 Product 페이지 **하단** 에 조용한 config로
  자리한다, 헤드라인이 절대 아님.
- **Deliverable은 다형적.** PR은 여러 artifact 타입 중 하나 — code / doc /
  image / slides / file / email. 각각 type 아이콘으로 렌더되고, 통합 surface는
  **"Delivery Report"** (Intent / What was built / How checked / Verdict /
  Risk / Artifact) — 타입별로 바뀌는 건 *What was built* 블록만. 코드 전용
  diff 가정을 대체한다.
- **Decide = AskUserQuestion, uniform.** BSVibe가 자체적으로 결정할 수 없는
  진짜 fork (taste / scope / direction / identity). retry/reframe/dismiss는
  별도 "escalation" surface가 아니다 — 진짜 fork가 "다음에 뭘 할까"일 때
  AskUserQuestion의 한 *종류* 다. 해소되면 tacit-knowledge 샘플 하나가
  BSage에 적립되고 다음 번 precedent로 노출된다.
- **Connector vs Skill** (BSage 자신의 추상을 재사용한 re-spec):
  - **Connectors = code/SDK 확장** (= BSage *plugin*). 외부 시스템 통합 —
    GitHub, Notion, Slack, Drive, Email, Figma, PowerPoint, Postgres. Settings
    아래에 산다.
  - **Skills = YAML-frontmatter + prompt 확장** (= BSage *skill*). BSVibe가
    수행할 수 있는 재사용 가능한 behaviour — "PRD from a one-liner",
    "security review checklist", "convert Figma to React". 라이브러리;
    founder가 설치 또는 저작 가능. MCP는 *전송 수단* 일 뿐 founder에게 나오는
    단어가 아님.
- **non-founder trigger의 자율성 = BSage Safe Mode** — 위험/내구성 있는
  output (push, merge, reply, send)은 founder 승인을 위해 큐잉; 새 노브 없음.
- **Workspace vs Product 연결 레벨** — credential은 workspace에 (Settings /
  Connectors), 구체적 리소스 바인딩은 Product에 (Product 페이지 하단 Sources).

## 5. Mockup — v2 canvas (현행)

Stitch project: `projects/8877688440056896819` ("BSVibe — UX v2"). 전반에
Notion craft bar: light `#FBFBFA`, hairline divider, typography 위계, 색은
상태 의미에만. Mobile이 주 타깃.

### 5.1 Founder surface (L0–L3 + Inside)

| Surface | 설계 의도 | Desktop | Mobile |
|---|---|---|---|
| **Brief** (L0 home) | Cross-product Glance — 상단 Needs you, plain-language product lane, Recently shipped (mixed artifact 타입). Direct 입력 박스 없음 — Direct는 전역 FAB / ⌘K. | `1be8ca9b0abd43aaba8092f6bea21b92` | `7238d7c2669e43eabfe5a80281d0d425` |
| **Product** (L1) — v2.2 | Per-product home — Needs you, In flight, Recently shipped, "What BSVibe knows". 하단 섹션은 **Resources** (이 레벨에선 Connectors에서 개명 — Workspace = accounts, Product = bound resources). 각 리소스에 3개 설정: selection · trigger · output mode (Safe Mode 기본). | `83b36b0573b2414a8b95d309a7f72d9b` | `6edc27c384de4327919b2a0c97b8d659` |
| **Delivery Report** (Run, L2) | 통합 glass-box: Intent / What was built (artifact 타입별 렌더) / How BSVibe checked this / Verdict / Risk / Artifact. Product 아래에 산다. "BSVibe remembers it." 로 마무리. | `a3a9667b8b3a4bd98f944d43d6f59c9e` | `977a0045aff04f699869304f181a1af9` |
| **Decide** | AskUserQuestion uniform — 진짜 fork (scope / taste / direction / identity), 큐레이션된 상호 배타 옵션과 1개 추천. precedent로 BSage에 적립. | `5bf54bdfd9f442f18edef5ef9aa25be7` | `172ed1723b634c5ab202f8c4055869d2` |
| **Triggered** | inbound GitHub issue / email이 자리 비운 사이 작업을 깨움. trigger + BSVibe의 framing + **Safe Mode** (승인 없이는 어떤 것도 BSVibe를 떠나지 않음). | `8f23216ae93f4b5ca7f317ef90e0ffea` | `7759cd902526419390760ea58c9a1c34` |
| **Inside** (graph) | BSage knowledge graph — Obsidian 스타일, 4 카테고리 색, 선택 노드 상세 (Retract / Edit). 우측 패널 / 하단 시트. | `2649e51e399b4857aa4b69bb1bbe7811` | `c87f9aae9d5747248101908f540d22b5` |
| **Skills** | YAML+prompt behaviour 라이브러리 (BSage skills). Installed + Gallery; founder가 저작 가능. | `7a6119ad1a2e49b5938d5d7ed104742b` | `fc190199862a4526b610e57f7dd0c1d0` |

### 5.2 Settings 서브 페이지

전체 공통 sub-nav: **General · Models · Connectors · Notifications · Account**.

| 서브 페이지 | 설계 의도 | Desktop | Mobile |
|---|---|---|---|
| **General** | Workspace 기본 — name, URL, default product, timezone, language, theme, ID. 그리고 작은 Danger Zone (workspace 삭제). | `a47bfce9698d416cbfb7306df4c22186` | `674477636d1f461683f487e986cde3c0` |
| **Models** — v2.2 | BSGateway 라우팅 (simple → local · substantial → opencode subscription) + 모델 계정 **Add/Remove** (Connectors와 같은 shape). 프로바이더당 multi-account (예: OpenAI key 2개). preference 토글 없음, "never touch" 캡션 없음. | `fef0f5aadc564d4784bf138951293723` | `548f67f5999f42ba8e83f8951a163b68` |
| **Connectors** — v2.1 | Workspace 레벨 code/SDK 통합 (BSage plugin). Connected + Available + **Add custom** 카드 (MCP server 지정 / BSage plugin SDK 통한 code module 로드). | `e68e365fa64c4996a2ae105e9bfd6083` | `c43c46beb7cc48f89bc7097ec7ff1931` |
| **Notifications** — v2.2 | Event (고정) × channel (동적 — 연결된 Connector에서 파생). **In-app** 만 항상 가용 (web/PWA, 고정 Push 없음). Email/Slack은 해당 Connector가 연결되면 등장. Quiet hours + 제품별 override. | `4b70653def5f412a973fd3b7a7dafc76` | `e8e78e29291748aeb9f9550e0b892fb1` |
| **Account** | founder 본인 — profile, plan & billing (opencode subscription 사용량), sign-in identity, active session. | `091cacacd0b6461b9e66152a19c7262b` | `bebd2c997b19477b92fba8b49418a770` |

### 5.3 보조 surface (modal · editor · picker)

| Surface | 설계 의도 | Desktop | Mobile |
|---|---|---|---|
| **Decisions inbox** | BSVibe가 물은 모든 fork 목록. Pending 탭에 row (question · product · category · age) → Decide 상세. 아래에 Recently resolved와 "now a precedent" 태그. | `1175801d57514ce58ce9bf3b81ff0cea` | `f60e5b3089d84d5eb798ab32d3832a27` |
| **Skill editor** | Skill 저작/편집 (YAML frontmatter + prompt 본문). IDE가 아닌 차분한 writing surface. Title · category · trigger · visibility · description · prompt editor · "Run on sample input" 테스트 패널. | `f47a2d1f562441f39ec4e34290ba0967` | `6acdbcc4dde24d4d86f0e2910d7b204b` |
| **Custom Connector** modal/sheet | Settings · Connectors의 "+ Add custom" 경로. 두 탭 (MCP server / Code module). MCP form (URL · auth · token · description) + Safe Mode 노트. | `29c453be74654be49574aaa01089ae7d` | `4f88899257674ceb92c4caacc5bf3284` |
| **Resource picker** modal/sheet | Product · Resources의 "+ Add resource" 경로. 연결된 Connector 별 그룹 (GitHub · Notion · Vercel · Email · …), 확장 가능, 체크박스 multi-select, 이미 bound된 항목엔 "Linked" 태그. | `c1973ab42d554339ae9f42b5105c6e52` | `6cf2b2954c8b43869b9a04026e479aa2` |

Desktop에서 modal은 페이지를 dim 처리한 중앙 오버레이. Mobile에선 full-screen
sheet가 된다 (하단 sticky 액션 바).

### 5.4 폐기 / 대체됨

- Legacy project `5698840467483031448` ("BSVibe — UX Moments") — v1.
- v2 Product `d162709e…` / `c8b26189…`, v2 Settings · Connectors `6e23f805…` /
  `4f99b6c4…` — v2.1이 대체.
- v2.1 Product `72d55af1…` / `efe8c4e8…` — v2.2가 대체 (Resources 개명 +
  per-resource 3-knob).
- v2.1 Settings · Models `48699736…` / `f81766f2…` — v2.2가 대체
  (add/remove accounts, multi-account, no preferences).
- v2.1 Settings · Notifications `d30b31fa…` / `a1e04f5b…` — v2.2가 대체
  (Connector에서 파생되는 channel, Push 제거).

## 6. 추가 IA lock (refinement 반복)

mockup은 v1 baseline moment 이후 3번의 refinement pass를 거쳤다. 그 pass에서
나온 주요 lock:

- **"Connector"는 workspace 전용.** Workspace Connectors = accounts /
  integrations.
- **Custom Connector는 1급** — Settings · Connectors에 명시적인 "Add custom"
  affordance (MCP server endpoint 또는 BSage plugin SDK module).
- **Settings는 5개 서브 페이지**: General · Models · Connectors ·
  Notifications · Account. 전체 공통 sub-nav.
- **Product 레벨 바인딩은 "Resources", "Connectors"가 아님.** Workspace =
  *accounts*; Product = *그 account에서 가져온 resource*. 부제:
  *"What this Product works on — chosen from your workspace Connectors."*
- **Product의 per-resource 3-knob**: **Selection** (어느 리소스) ·
  **Trigger** (inbound 활동이 작업을 깨우는지 — ON/OFF + 필터) ·
  **Output mode** (Safe Mode / Direct — 기본 Safe Mode). founder는 보통
  selection만 건드림; default가 안전함.
- **Models = 계정 Add/Remove, 프로바이더당 multi-account.** Connectors와
  같은 shape (동적 리스트). Connect/Disconnect 없음. preference 토글 없음
  (cloud fallback, privacy mode, default tier 모두 폐기). Routing + Accounts
  만.
- **Notifications channel은 Connector에서 파생.** **In-app** 만 항상 가용
  (web/PWA, 네이티브 모바일 앱 아님 — 고정 Push 없음). Email/Slack 등은
  해당 Connector가 연결되면 matrix 컬럼으로 등장; 제거되면 사라짐. Event는
  고정 유지. 캡션이 founder에게 채널 추가 위치를 알려줌.

## 7. 다음

- 26개 v2 화면에 대한 founder 리뷰 (Stitch project `8877688440056896819`).
- Delivery Report용 artifact 타입별 **What was built** 렌더링
  (doc preview / image / slide thumbnail / email body / file metadata).
- Stitch design-system 추출 → 통합 design token.
- 이 surface들을 서비스하는 OS workflow는
  [docs/architecture/workflow-backend.md §1](docs/architecture/workflow-backend.md) 에 lock 됨.
- Ratchet 철회는 UX home (Inside-graph 노드 상세 "Retract") + backend home
  (Workflow §11.4 BSupervisor signal)을 가짐.
- Parked: 타깃 사용자, 슬로건 — moment + Synthesis §9의 경쟁 분석으로 이제
  충분히 양분됨.
