# BSVibe OS — Strategy Synthesis

> Date: 2026-05-19 (2026-05-20 정리 반영)
> Status: **Strategy SoT.** 형제 문서:
> [docs/architecture/ux-design.md](docs/architecture/ux-design.md) (UX SoT)
> 및 [docs/architecture/workflow-backend.md](docs/architecture/workflow-backend.md)
> (workflow + backend SoT). 원 기록: `Trust_Measurement_Design_2026-05-19.md`.
> 결정은 §11에서 **[locked]** / **[open]** / **[parked]** 로 표기. 아키텍처
> open은 모두 닫혔고, 남은 [parked]는 타깃/슬로건.

---

## 0. 이 문서가 존재하는 이유

리디자인은 "BSNexus 리디자인"으로 시작했다. 2026-05-19 대화에서 frame이 두
단계 커졌다: BSNexus는 제품이 아니다 — founder가 **BSVibe** 라 명명한
**AI agent OS** 의 한 layer이고, 팔리는 것은 그 OS다. 이 문서는 화면 레벨
작업이 재개되기 전 founder가 확정한 ("이건 맞아") synthesis다. 앞선 anchor ①
proof-surface 설계는 본질적으로 맞지만 한 단계 낮은 고도에서 그려졌다; §12에서
어떻게 재배치되는지 다룬다.

---

## 1. Origin — Bet B 피벗

`Trust_Measurement_Design_2026-05-19.md` (+ ADDENDUM) 출처: Notion 3.5 방향성
위기는 오진으로 진단됐다. 진짜 블로커는 **신뢰 (trust)** 다. founder는
**Bet A → Bet B** 로 피벗했다:

- **Bet A** — trust = 신뢰성 (reliability). 모델 경쟁에서 이기고, 위임하고,
  손 떼기.
- **Bet B** (채택) — trust = **transparency + control**. 가시성 + HITL +
  커스터마이징으로 founder가 불완전한 agent를 *감독* 할 수 있게 리디자인.
  glass-box 명제.

리디자인 범위는 product / experience / IA 에 한정 — engine greenfield는 **아님**
(G0–G6에서 이미 했음).

---

## 2. 제품 — BSVibe, AI agent OS

**한 줄:** BSVibe는 한 사람이 동시에 여러 제품을 운영하기 위해 사용하는
운영 체제 (operating system) 다. 실제 OS가 여러 프로그램을 — glanceable,
scheduled, isolated, 필요할 때만 개입 — 운영하듯이, BSVibe는 여러 AI
work stream을 운영한다.

**해결하려는 원래 통증** (founder 본인 표현): Claude Code를 컴퓨터 앞에서
babysit 하는 것 — 보고, 확인하고, 지시하는 것 — 은 지긋지긋하다, *특히*
여러 개를 동시에 돌려야 하기 때문이다. founder는 **제품을 양산하고 싶다
(churn out products)**. 그러려면 단일 앱이 아니라 OS가 필요하다.

**비즈니스 모델:** BSVibe (OS)가 **외부에 판매되는 SaaS다.** 그것으로 양산되는
제품은 별개 문제.

**Naming [locked]:**

```
BSVibe          = the AI agent OS — the product sold
 ├ BSNexus      = execution layer / kernel — runs the work
 ├ BSage        = personalization — the tacit-knowledge ontology
 ├ BSGateway    = power — LLM routing / cost
 └ BSupervisor  = safety — guardrails, ratchet enforcement
```

회사명 = 제품명, 당분간은. `bsvibe.dev` 와 일관.

---

## 3. 공간 구조 — 4개 zoom 레벨

BSVibe의 네비게이션은 "4 제품"이 아니라 **zoom**. desktop → app → window 처럼:

| Level | 무엇 | OS 비유 | 답하는 질문 |
|---|---|---|---|
| **L0 Fleet** | 모든 제품을 한눈에 | desktop / task manager | "전부 뭘 하고 있나? 나를 필요로 하는 것은?" |
| **L1 Product** | 한 제품 | 실행 중인 앱 | "이 제품 어디까지 왔나?" |
| **L2 Run** | 한 단위 위임 작업 | process / window | "어떻게 되었나 + proof" |
| **L3 Process** | Run의 실행 내부 | thread / call stack | "agent가 round 단위로 뭘 했나" |

Anchor ① proof surface (`Redesign_Anchor1_…`)는 **L2**. 빠져 있던 조각은
**L0 Fleet** — "한 번에 여러 개 운영"의 직접적 답. 따라서 anchor ①
(사용 가시성)은 **두 고도** 를 갖는다: Fleet (여럿 skim) + Run (하나 증명).

---

## 4. 4 layer — 명사, 형용사, 그리고 루프

layer는 nav destination이 **아니다**. OS에서 결코 "CPU를 방문"하지 않듯이:

- **BSNexus (execution)** → 보이는 **명사** 를 생산 — 제품, Run, process.
- **BSGateway (power)** → 모든 Run에 붙는 **형용사** — `cost`.  ⟨이름 폐기 2026-05 → `dispatch/resolver.py`⟩
- **BSupervisor (safety)** → 모든 Run에 붙는 **형용사** — `guardrail status`,
  그리고 **ratchet 집행자 (enforcer)** (§5).
- **BSage (memory)** → 모든 Run에 붙는 **형용사** — `what was known going in`
  — 그리고 tacit-knowledge ontology (§6).

Gateway / Supervisor / Sage는 **별도 앱 없음, 별도 로그인 없음**. 데이터는
Fleet / Product / Run 뷰에 ambient하게 짜여 들어간다. OS 메타포가 anchor ④
(tight coupling)를 *강제* 한다 — 더 이상 옵션이 아니다.

다만 4 layer는 정적이지 않다. **loop** 가 있다 (§5).

---

## 5. 척추 — trust ratchet

**전제** (founder 입력): LLM은 *만드는* 데는 능숙하지만 *자신이 만든 것을
리뷰* 하는 데는 약하다. 따라서 리뷰는 LLM에 기댈 수 없다. **기계적이고,
누적적이며, 일방향** 이어야 한다.

**"Ratchet"은 *속성 (property)*, 엔티티가 아니다.** 이것은 함께 단조 증가하는
신뢰를 만들어내는 두 일방향을 가리킨다:

1. **work loop이 일방향** — *plan → act → verify → (다음 반복)*.
   verify 실패가 되돌리지 않는다; 새로운 plan을 트리거할 뿐이다. 각 반복이
   누적된다.
2. **BSage 지식이 일방향** — 교정, 결정, 회고는 ontology에 *추가만* 된다.
   graph는 단조 증가한다.

```
BSNexus runs the work (agent loop)
   │   failure / mistake / founder corrects via a Decision
   ▼
BSage captures the correction (canonicalize · dedup · graph-link)
   │
   ▼
On the next trigger, BSage retrieval surfaces the relevant captured
patterns at verify-declaration time — they enter the verification
contract naturally, not via a separate "promotion" step.
```

4 layer는 4개의 정적 하위 시스템이 아니라 **학습 루프 (learning loop)** 다.
이것이 수직 통합 해자의 진짜 형태다.

**왜 ratchet 속성이 모든 것을 해소하는가:**

- **원래 통증.** babysitting이 아픈 이유는 *같은 것을 반복적으로* 교정하기
  때문이다. ratchet은 각 실수가 **한 번** 감독된다는 뜻이다 — BSage에
  들어가고, 미래의 verify가 그것을 retrieve한다. 감독 부담은 시간이 지나며
  **감쇠 (decay)** 한다. "위임하고 손 떼라"를 약속할 수 있는 유일하게 정직한
  방법이다.
- **Bet B 완성.** Bet B의 미해소 갭은 "결코 개선되지 않는 agent에 대한
  아름다운 창은 여전히 실패한 제품"이었다. ratchet 속성이 답이다 — founder
  감독이 BSage에 **누적** 되고, agent는 *founder 기준을 향해* 개선된다.
- **Notion 차별화의 결정적 우위.** Notion은 정적이다 — PR을 보여주고
  믿는다. BSVibe는 **같은 실수를 두 번 할 수 없는 OS** 다. document substrate
  에는 학습 루프를 앉힐 자리가 없다. 쉽게 복제 불가.
- **Personalization = founder별 BSage graph.** 두 BSVibe 사용자가 서로 다른
  ontology를 보유한다 — 당신의 실수, 당신의 컨벤션. 그게 *바로* personalization.
- **깨진 벤치마크 흡수.** genuine/false-verified rate는 일회성 A/B가 아니라
  누적 속성으로 추동되는 **단조 상승 추세** 가 된다. 측정 *자체* 가 제품
  behavior다.

---

## 6. BSage — tacit-knowledge ontology

BSage 전체 정의 =

> **founder의 모든 tacit knowledge를 graph + markdown ontology로 외재화한 것**
> (markdown = source of truth — BSage 포지셔닝 lock-in). 외재화된 founder의 mind.

모든 것이 여기로 들어간다 — preference, convention, taste, 판단 기준,
결정 뒤의 *이유*, 도메인 지식, wall 같은 behavior를 만드는 corrections-as-rules.
단일 저장소, 티어 없음.

### 어려운 문제와 설계 원칙

tacit knowledge는 정의상 **글로 적혀 있지 않다.** founder에게 "모든 걸
문서화하라"고 요구하면 실패한다 — 그게 정확히 Notion의 지식이 얇고 stale한
이유다 (수동 저작). 따라서:

> **설계 원칙: founder는 결코 지식을 저작하지 않는다. BSVibe가 founder의
> 자연스러운 command 행위에서 추출한다.**

모든 founder 상호작용은 **이중 목적 (dual-purpose)** 이다 — 당면한 것을
해소하고 *동시에* tacit-knowledge 샘플 하나를 적립한다. 캡처 moment:

| Moment | 누출되는 tacit knowledge |
|---|---|
| **Direction** | 무엇이 중요한지, 우선순위, framing, 어휘 |
| **Decision** | 판단, taste, 위험 감내, *이유* |
| **Correction / Reframe** | agent가 위반한 표준 — 가치 높음 |
| **Review** (approve / reject) | 품질 기준 |
| **기존 artifact** (repo, 과거 PR, doc, chat) | 이미 대량 적립된 tacit knowledge — ingestible |

### 단일 retrieval, BSage-가중

**티어 시스템 없음**, "ratchet으로 승격" 파이프라인 없음. BSage는 이미 내부적으로
가중 retrieval을 한다 (wall처럼 behave하는 패턴은 canonicalization, soft
context는 semantic search — 하지만 호출자에게는 단일 쿼리로 보임). verify
선언 시점에 BSage retrieval이 관련된 캡처 패턴을 가져오고, work LLM이 선언한
것과 함께 verification contract에 자연스럽게 합류한다. 티어 고정은 BSage의
동적 ontology를 위배한다.

기계적-wall behavior는 BSage의 canonicalization에서 나온다: 반복 교정된
패턴은 canonical 노드가 되고, canonical retrieval은 그 시그널이 발화될
때마다 패턴이 표면화될 만큼 결정론적이다. 별도 "guards" 엔티티 없음,
opt-in 승격 없음.

### Global vs personal — 해소됨

보편 패턴 (예: gstack `*-trap` skill)은 공유 자산; repo/founder 특정
컨벤션은 개인적. **BSage의 기존 category 기능이 이 분기를 처리** — 새
메커니즘 아님. **[closed]**

### Ontology도 glass-box

ontology가 markdown (SoT)이기 때문에, founder는 자신의 외재화된 mind를
열고, 검사하고, 교정할 수 있다. 이것도 Bet B 투명성이다.

---

## 7. Glass box + ontology = 하나의 surface, 두 면

Anchor ① (L2 Run proof surface)와 BSage는 **두 방향에서 본 같은 surface** 다:

- **바깥 방향 (가시성):** founder가 Run *안을* 들여다본다 — agent가 한 것,
  증거, verdict.
- **안쪽 방향 (지식):** founder가 그 surface에서 보인 *반응* — correct,
  approve, reject, reframe — **이** BSage 추출 이벤트다.

glass box는 동시에 **inspection window** 이자 **knowledge-capture instrument**
다. founder가 들여다보고; BSVibe는 그들이 반응한 것을 캡처한다. 한 화면, 두 일.

---

## 8. Shell — founder-metaphor 4개 생존

lock된 maximally-simple 규칙 유효: founder surface는 **Direct / Brief /
Decisions / Inside**, 절대 5번째 없음. zoom 구조에 대응:

- **Brief** = L0 Fleet — 홈.
- **Decisions** = 가로지르는 개입 인박스 (paused된 Run으로 링크).
- **Inside** = L2 Run / L3 Process 진입 — proof surface.
- **Direct** = 입력, 어느 레벨에서나 가용.

인프라 (budget, round, ratchet 내부)는 비가시 기계 유지. founder는 *효과* 를
본다 (신뢰 상승, 실수 비반복), "ratchet management panel"이 절대 아니다.

---

## 9. 경쟁 지형 & 차별화

2026-05-19 리서치 — 3차례 독립 소싱 리서치 sweep (Notion; Anthropic/OpenAI
agent 제품; Paperclip + 더 넓은 set). Notion 3.5 "Developer Platform"이
2026-05-13에 발표됨 — 위기를 촉발한 웨비나; 리서치는 **Notion이 verify하지
않는다** 는 Trust 문서의 판독을 확인.

### 9.1 지형

| Product | 자칭 카테고리 | Verifies? | Learning loop? | Shape | Target |
|---|---|---|---|---|---|
| **Notion 3.5** | "hub / orchestration layer" | ✗ — audit log + human approval (propose→approve) | ✗ | knowledge OS, agents bolted on | teams (Biz/Ent, per seat) |
| **Paperclip** | "control plane for AI labor / AI company" | ✗ — audit log only | ✗ | management console (org chart, budgets, roles) | multi-agent operators |
| **OpenAI Frontier** | "manage AI agents like employees" + feedback loop | ? | claimed only (unproven, enterprise) | enterprise agent platform | enterprise |
| **Claude Code web / Cowork / Managed Agents** | dev tool / assistant / infra | human review | ✗ — static CLAUDE.md | three split tools | developers / non-technical |
| **OpenAI Codex** | coding agent | △ — engineer-facing log citations + auto PR review | ✗ — static AGENTS.md | dev tool | developers |
| **Devin / Factory** | "autonomous AI engineer" | △ — self-verify (internal, not shown to delegator) | ✗ | autonomous coding agent | dev teams / enterprise |
| **Cursor / Conductor / Lovable** | IDE / app builder | ✗ — human reviews diff | ✗ | dev tool | developers / non-technical |

**모든 출하 중인 경쟁자에서 두 컬럼이 비어 있다:** **founder가 읽을 수 있는
per-result proof surface**, 그리고 **cross-task learning loop**. learning loop은
DIY 스크립트 (Ralph Loop, lessons.md)와 연구 논문으로만 존재. 3차례 리서치 sweep이
이 지점에 독립적으로 수렴.

### 9.2 의미

- **wedge가 검증됨.** proof + learning loop (§5 ratchet)은 진짜로 열린 갭이다.
- **"AI agent OS / AI company / control plane"은 *외부* 카테고리로 선점됨.**
  Paperclip (~2.5개월 만에 GitHub stars ~66k)이 그 어휘를 차지; Notion은
  "hub"를 차지. → **OS frame은 내부 아키텍처용으로만 유지.** 외부 pitch는
  wedge로 리드 — *verified work + agents that stop repeating mistakes* —
  절대 "an OS for your AI company"가 아니다.
- **"AI agent에 위임"은 table stakes** — $20 구독에 묶여 들어감. pitch가 아님.
- **Codex가 proof wedge에 가장 가까운 위협** — terminal log와 test output을
  인용한다. 하지만 그건 *엔지니어가 읽는 것*. BSVibe의 proof는 **founder가
  읽을 수 있어야** 한다 — 그게 방어 가능한 선.
- **프론티어 lab을 타라, 싸우지 마라.** 그들의 모델, 샌드박스, 디스트리뷰션이
  실행과 비용에서 이긴다. BSGateway가 그들로 라우팅 (§13); 해자는 trust
  layer, 실행이 아니다.
- **Anti-Paperclip은 이제 taste가 아니라 카테고리 차별화.** Org chart,
  budget, role UI는 Paperclip의 *제품 전부*. BSVibe는 *proven work* 를
  보여주지 *관리할 회사* 를 보여주지 않는다. hard UX constraint.
- **Target signal:** Notion = team/per-seat, Frontier = enterprise,
  Paperclip = multi-agent operator, lab = developer. **여러 제품을 가로질러
  위임하는 1인 founder는 모두에게서 미충족** — 빈 자리.

### 9.3 삼각형

- **Notion** = 회사가 **생각** 하는 곳 — 지식; 검증 없음, 학습 없음;
  team 형태.
- **Paperclip** = AI 노동을 **관리** 하는 곳 — org chart, audit; 검증 없음,
  학습 없음.
- **BSVibe** = 작업이 **shipped + proven** 되는 곳 — 실행 + founder가 읽을
  수 있는 proof + 복리로 쌓이는 ratchet; 기계 비가시.

복제 불가 핵심 = **learning loop** (§5): proof → BSage → ratchet. document
substrate (Notion)도 얇은 governance layer (Paperclip)도 이걸 앉힐 자리가 없다.

---

## 10. 지표

- **Founder touch time** (북극성)은 또한 **knowledge-deposition rate** 다 —
  1분의 touch가 미래의 모든 touch를 감소시킨다. 비용은 일방향으로 감쇠한다.
- **Trust** = genuine-verified rate; 단조 상승해야 한다 (ratchet).
- 측정은 사이드 하니스가 아니라 제품 behavior다 — m0 벤치마크 실패는
  Fleet/Trust 가시성에 흡수된다 (anchor ① §7).

---

## 11. 결정 상태

**[locked]**
- Bet B (trust = transparency + control; glass box).
- BSVibe = AI agent OS; 그것이 판매되는 SaaS다.
- Naming: 상위 BSVibe, 하위 layer로 BSNexus / BSage / BSGateway / BSupervisor.
- 4 zoom 레벨: Fleet / Product / Run / Process.
- 4 layer = 명사 + 형용사, ambient하게 짜여 들어감 — 별도 앱/로그인 없음.
- trust-ratchet 척추; 4 layer는 learning loop.
- BSage = founder의 tacit-knowledge ontology (graph + md); 단일 가중 retrieval
  (티어 시스템 없음); "founder는 결코 저작하지 않는다" 추출 원칙.
- **Ratchet은 *속성*, 엔티티가 아니다** — agent loop의 일방향 + BSage의 단조
  누적. 티어 아님, guard store 아님, 별도 집행 메커니즘 아님. 기계적-wall
  behavior는 BSage canonicalization + verify-declaration retrieval에서 발현.
- Glass box + ontology = 하나의 surface, 두 면.
- founder-metaphor 4 (Direct/Brief/Decisions/Inside)이 shell로 유지.
- **"AI agent OS"는 *내부 아키텍처* frame 한정.** 외부 pitch는 wedge로
  리드 — verified work + 비반복 실수 — OS/company/control-plane 카테고리
  아님 (Paperclip이 선점; §9 참조).
- 경쟁 삼각형 (§9.3); learning loop이 복제 불가 핵심.
- Global vs personal learning = BSage의 category 기능.
- **모델 티어링 [§13]:** BSGateway(→ 現 `dispatch/resolver.py`)가 티어별 라우팅 — 간단한 chore → local
  LLM; substantial work + orchestration → opencode subscription 모델,
  새 baseline / minimum spec. local-Ollama-only minimum-spec lock 폐기.
- **non-founder trigger 자율성 = BSage Safe Mode** — queue-only, 위험/내구성
  있는 output에 대한 pull-based 승인. 새 trust-tier 노브 없음.
- **UX-first 방법 [§15]:** workflow를 도출하기 전에 founder 경험을 먼저
  설계 — 그 반대가 아님.

**[open]** — 없음. 모든 아키텍처 open은 하위 SoT에서 해소됨:
- ~~추출 정밀도~~ → 아키텍처가 아닌 구현 관심사. 3-tier discipline framing은
  폐기됨 (§6); BSage는 단일 가중 retrieval과 canonicalization을 사용. 추출
  품질은 이제 코딩 시점 / 관측성 관심사이지 설계 open이 아니다.
- ~~Ratchet 철회~~ → 처리됨. UX는 Inside graph에 founder retract affordance
  를 가짐 (UX Design §3.3 보조 surface); Workflow 문서 §11.2/§11.4가
  해당 canonical 노드에 대해 BSage로의 BSupervisor 철회 시그널을 명명한다.
- ~~OS workflow 잠정 안~~ → **Workflow + Backend 문서 §1이 lock된 workflow.**
  Receive → Frame → Agent loop → ε (연속적인 Deliver/Settle 사이드 채널 동반).
  이 synthesis는 더 이상 workflow draft를 끌고 가지 않는다.

**[parked]** (나머지 이후 자연스럽게 결정)
- 타깃 사용자 — 다만 §9.2가 1인 founder가 빈 자리임을 시사.
- 슬로건.

## 12. 모델 티어링 [locked 2026-05-19]

> **📌 주체 이름 정정 (2026-08-18) — 결정은 살아 있고, 주체가 바뀌었다.**
> 이 절의 **BSGateway** 는 2026-05 통합(4제품 → `bsvibe-app`)으로 **폐기된 이름**이다.
> 티어별 라우팅은 지금 **`backend/dispatch/resolver.py`** 가 한다
> (`caller_id` × workspace → ModelAccount; 룰은 `run_routing_rules`).
> **결정 자체(간단한 작업 → 저렴한 모델, 실질 작업 → 구독 모델)는 유효하다.**
> 원문은 감사 근거로 **그대로 보존**한다 — 이 문서의 §5/§6/§11 인용 위에 트랙 A 전체가 서 있다.

전체 multi-step orchestration을 local 모델 (qwen3-coder:30b)로 돌린 것은
과욕이었다 — decomposer / budget / round-cap struggle이 그 증거.

- **BSGateway가 티어별 라우팅:** 간단한 chore (분류, 소소한 편집, formatting)
  → local LLM; substantial work step + orchestration → **opencode subscription
  모델** = 새 **baseline / minimum spec**.
- 품질 기준은 local qwen3가 아니라 opencode baseline 대비 검증.
- 이는 보류됐던 "Bet B Approach B tiered routing"의 부분 채택.
- `minimum_spec` lock-in (local Ollama 기준)을 대체.

## 13. OS workflow — Workflow + Backend 문서에 lock

OS workflow는 이 문서의 초기 draft에서 잠정적으로 스케치됐고, 이후
**[docs/architecture/workflow-backend.md §1](docs/architecture/workflow-backend.md) 에 lock 됨**:
*Receive → Frame → Agent loop ↻ → ε*, 그리고 **Deliver** 와 **Settle** 는
agent loop이 시종일관 방출하는 연속적인 사이드 채널.

이 섹션은 포인터로 유지; 이전의 9-stage draft (Trigger → Intake → Frame/Route →
Decompose → Execute → Verify+Ratchet → Decision → Deliver → Settle)는
lock된 3+ε 형태에 **대체** 됨. 특히:
- "Verify+Ratchet"은 하나의 step, 하나의 조립된 contract (work-LLM 선언 +
  BSage retrieval이 선언 시점에 병합). 별도 Ratchet check layer 없음
  (Ratchet은 *속성*, 엔티티 아님 — §11 참조).
- Deliver와 Settle은 종단 stage가 아니라 *연속적* 사이드 방출.
- Decision은 종단 상태가 아니라 mid-loop pause-and-resume.

## 14. UX-first 방법 & founder moment

UX-first (founder의 교정): workflow를 먼저 설계하면 UX가 레거시 BSNexus
파이프라인의 종속 변수가 되고 4-product 통합이 열어준 선택지를 놓친다. 따라서
경험을 먼저 — founder의 반복적 **moment** 에서 — 설계하고, workflow (§13)는
그것을 서비스하기 위해 도출된다.

6개 moment (UX 설계 scaffold — 상세는
[docs/architecture/ux-design.md](docs/architecture/ux-design.md) 참조):

1. **Direct** — 아이디어/지시를 최소 마찰로 던짐.
2. **Passive trigger** — founder가 자리 비운 사이 email / issue가 작업을 깨움.
3. **Glance (Brief / Fleet)** — "전부 뭘 하고 있나? 나를 필요로 하는 것은?"
4. **Decide** — founder를 필요로 하는 몇 건의 결정.
5. **Review (proof)** — "정말 끝난 건가? 믿을 수 있나?"
6. **Inside** — "왜 그렇게 했지 / BSVibe가 나에 대해 뭘 아나?" + ontology 교정.

Hard UX constraint (§9에서): 절대 관리 콘솔이 되지 않는다. *proof* 로의
glass box, 절대 org chart 아님. Craft bar = Notion 수준의 calm.

## 15. 다음 — implementation-ready

문서 세트가 이제 implementation-ready. 형제 SoT 3개:

- **이 문서** — 전략 (포지셔닝, layer, 경쟁, [parked] 타깃/슬로건)
- **[docs/architecture/ux-design.md](docs/architecture/ux-design.md)** — UX surface, 6 moment, Stitch mockup
- **[docs/architecture/workflow-backend.md](docs/architecture/workflow-backend.md)** — workflow, backend topology, data model, 스키마, GDPR, 보상, bootstrap, 시퀀스 다이어그램, Phase 0 acceptance criteria

구현은 Workflow §12 — Phase 0 monorepo skeleton 에서 시작.
