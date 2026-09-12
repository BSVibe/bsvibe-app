# `pipeline` 제거 + 라우팅 재설계 — 설계 브리프 (2026-08-24)

> **이 문서는 다음 세션의 시작점이다.** 코드 변경은 아직 없다.
>
> 형님 판단 (2026-08-24, 원문):
> > *"애초에 이게 있는거 자체가 문제인것 같은데. design_then_impl이 나온거는 내가 세팅한
> > llm routing 때문 아냐? 이건 아주 작은 예시 중 하나일 뿐이지, 다른 사람들도 이렇게
> > 세팅한다는 증거는 아냐. 지금은 내 예시에 과최적화 되어 있고, 애초에 pipeline 자체가
> > 있으면 안될듯 한데"*
>
> 그리고 내가 "순수 삭제"로 좁히려 하자 잡으셨다:
> > *"오해가 큰 듯 한데. pipeline을 지워도 라우팅 룰은 동작해야 해. 라우팅 룰은 애초에
> > 자연어잖아. 사용자의 의도와 현 작업을 매핑해서 작업을 넣는 기능이 필요한거야.
> > 즉 지금은 순수 삭제만 하면 되는게 아니라, 작업 방향이 잘못 되었으니 정리하고
> > 다시 설계해야 할 것 같단거야"*

---

## §0 한 줄 요약

**`pipeline` 은 프레임이 고정 어휘 2개로 미리 맞히는 예측이고, 그것 없이도 살아야 하는
라우팅 기능(자연어 의도 ↔ 현 작업 매핑)은 이미 코드에 있는데 한 번도 안 쓰였다.**

---

## §1 실측 — 전부 prod 에서 직접 셌다 (2026-08-24)

### 1.1 `pipeline` 이 값을 내는 이유는 형님의 룰이다

prod `run_routing_rules` **3개**:

| 룰 | 조건 |
|---|---|
| `design stage → claude opus` | `stage == "design"` |
| `impl stage → claude sonnet` | `stage == "impl"` |
| `default → claude sonnet` | (없음) |

**둘이 형님이 손수 만든 stage 룰이다.** 그 룰이 없으면 `design_then_impl` 은 설계 런과
구현 런을 **같은 모델로 두 번 도는 것**이 된다 — 런 하나가 둘이 되는 비용만 남는다.

### 1.2 그 32건도 깨끗하지 않다

```
frame.pipeline 분포:  single 169 · design_then_impl 32 · (없음) 19
design_then_impl 내부: stage 없음(설계) 21 · impl 11     ← 설계 21 중 10건은 구현으로 못 이어짐
impl 런의 결말:        review_ready 7 · cancelled 4
```

### 1.3 폴백은 판정하는 척만 한다 (감사 A11)

`_resolve_pipeline` 은 LLM 이 유효한 값을 안 주면 `_derive_pipeline` (키워드 규칙)으로 간다.
`_BUILD_INTENT_WORDS` 는 **영어 15단어**뿐 — `build`·`implement`·`feature`·`app`·`application`·
`service`·`system`·`design`·`refactor`·`integrate`·`endpoint`·`api`·`module`·`component`·`pipeline`.

prod 의 `framed_intent` 는 **전부 한국어**다(실측 표본 6/6). ∴ **이 폴백은 오늘 항상
`single` 을 낸다.** 상수인데 판정처럼 생겼다.

⚠️ 옆 필드 `path_classification` 은 **같은 키워드 휴리스틱을 이미 삭제**했다 —
사유가 코드에 남아 있다: *"'설명해줘' / 'explain X' 를 못 알아보고, 다른 언어엔 할 말이
전혀 없었다."* 그리고 fail-closed(`FrameUnclassifiedError`)로 갔다.

### 1.4 형님이 말한 그 기능은 이미 있는데 **0행**이다

| | |
|---|---|
| `intent_definitions` | **0행** |
| `intent_examples` | **0행** |
| 런 220건의 `classified_intent` | **전부 없음** |

`classified_intent` 는 **사용자가 정의한 intent 를 임베딩으로 현 작업에 매핑**하는 축이고
`ALLOWED_FIELDS` 에 있다. authoring 표면도 REST(`POST /api/v1/intents`)·MCP
(`bsvibe_intents_create`) 양쪽에 있다.

**그런데 아무도 intent 를 만든 적이 없어 분류기는 한 번도 돌지 않았다** —
`_needs_classified_intent` 가 *"그 필드를 키로 쓰는 룰이 있을 때만"* 분류기를 돌리는데
그런 룰이 없다. (전형적인 half-wired subsystem.)

### 1.5 두 축이 같은 질문에 답한다

| 축 | 누가 정하나 | 어휘 |
|---|---|---|
| `pipeline` / `stage` | **시스템이 예측** | 고정 2값 (`single` / `design_then_impl`) |
| `classified_intent` | **사용자가 자연어로 정의** | 사용자 어휘 |

형님이 말한 *"라우팅 룰은 애초에 자연어"* 는 **두 번째**다.
첫 번째는 그 위에 얹힌, 예측하는 **두 번째 소스**다 (INV-7 · 툴 표면 SoT 가 반복 지적한 모양).

---

## §2 삭제 반경 — 7파일 · 약 300~500 LOC

```
frame.py            PipelineKind · pipeline 필드 · pipeline_reason ·
                    _derive_pipeline · _BUILD_INTENT_WORDS · _resolve_pipeline ·
                    프롬프트 2줄
agent_runner.py     _maybe_spawn_impl_run · _design_artifact_refs
_loop_context.py    _is_design_stage · design_directive_message · design_seed_message
agent_briefing.py   설계 지시 부분 (129 LOC 중 일부)
handoff.py          read_design_context · capture_design_spec_text (130 LOC)
_drive_loop.py      DESIGN 단계 spec-only 분기
engine.py           ALLOWED_FIELDS 의 `stage` · `pipeline`  ← ⚠️ §3 참조
```

`ALLOWED_FIELDS` 11개 중 **둘만** pipeline 파생이다. 나머지 9개는 그대로 산다:
`artifact_type_hint` · `path_classification` · `skill_match` · `intent_text` ·
`product_id` · `caller_id` · `estimated_tokens` · `classified_intent` · `detected_language`.

---

## §3 형님 판단이 필요한 지점 — **다음 세션은 여기서 시작한다**

### Q1. 형님의 니즈를 무엇으로 표현하나

*"설계는 opus, 구현은 sonnet"* = **어려운/생각이 필요한 작업은 강한 모델로.**
`stage` 없이 이걸 어떻게 쓰나? 후보(전부 **기존** 필드다 — 새 축을 만들지 마라):

- `classified_intent` — 형님이 intent 를 정의하면 그 어휘로 룰을 쓴다 (0행 문제를 먼저 풀어야)
- `estimated_tokens` — 크기 프록시
- `intent_text` — 텍스트 연산 (자연어 그대로)
- `artifact_type_hint` — 산출물 종류

### Q2. `classified_intent` 가 0행인 이유는 무엇인가

셋 중 어느 것인지 **측정으로** 갈라야 한다:
1. authoring 이 발견이 안 된다 (UI/온보딩 문제)
2. 만들 이유가 없었다 (룰이 없으면 분류기도 안 도는 순환)
3. 그 축도 잘못된 모양이다

### Q3. 형님의 룰 2개를 어떻게 이어주나

`design stage → opus` / `impl stage → sonnet` 은 `stage` 가 안 생기면 발화하지 않는다.
⚠️ **룰은 사용자 설정이다** — 코드가 임의로 지우면
[[feedback_user_config_is_not_a_defect]] 에 어긋난다. 마이그레이션할지, 알리고 형님이
직접 정리할지 정해야 한다.

### Q4. prod 데이터 32건 (설계 21 · 구현 11)

payload 에 `pipeline` / `stage` 가 박혀 있다. 남길지, 정리할지.

---

## §4 하지 말 것 (이 세션에서 배운 것)

> 툴 표면 SoT §7 이 기록한 공통 실패: **"결함을 볼 때마다 새 구조로 답하려 했다.
> 세 번 다 진짜 답은 이미 있는 것을 쓰거나, 있는 것을 없애는 것이었다."**

- **새 필드/새 enum/새 플래그를 만들지 마라.** `ALLOWED_FIELDS` 9개로 표현되는지 먼저 보라
- **프레임에게 예측시키지 마라.** *"이 작업이 설계를 거쳐야 하나"* 는 해보면 아는 것이다
- **n=1 을 일반화하지 마라.** 형님 세팅 하나가 근거가 될 수 없다 (이 문서의 출발점)

---

## §5 연결

- 툴 표면 SoT: `docs/design/tool-surface.md` (같은 종류의 예측을 이미 폐기)
- 감사 처리: `internal-docs:BSVibe_Audit_Remediation_2026-08-21.md` (A11 이 이 트랙으로 승격됐다)
- `#770` — 룰의 *존재*가 design→impl 체이닝의 피처 플래그를 겸했던 사건

---

# 파트 2 — 실측 정정 + 설계 확정 (2026-08-24 후반 세션)

> §1~§5 는 **작성 당시의 이해**다. 아래 §6 이 그 중 둘을 실측으로 뒤집는다.
> §7 이 형님이 확정한 설계다. **여기가 SoT 다.**

## §6 실측 정정 — §1.1 과 §3.Q2 가 틀렸다

전부 prod (`bsvibe-prod-postgres-1`) 에서 직접 셌다.

### 6.1 🔴 형님 워크스페이스에는 라우팅 룰이 **0개**다

| 워크스페이스 | 런 | 룰 |
|---|---|---|
| `5fa3494c` **qazasa123's** — BSVibe · BStockReport, 형님이 실제로 쓰는 곳 | **169** | **0** |
| `6515bfc2` admin's — 08-09 이후 유휴 | 54 | **3** |

`bsvibe_run_routing_rules_list` (사용자 표면) → `[]`.

∴ **§1.1 의 *"pipeline 이 값을 내는 이유는 형님의 룰이다"* 는 형님이 일하는
워크스페이스에서 성립하지 않는다.** 룰은 유휴 워크스페이스에 있다.

### 6.2 🔴 기록된 라우팅 판정 **21건 전부** `workspace_default → sonnet`

`execution_run_activities.activity_type='routing_decision'` (기록 시작 2026-08-18).
그 중 design 스테이지 **3건** · impl 스테이지 **2건** 포함. `explicit_rule` **0건**.

∴ **`design stage → opus` 는 실제 런을 한 번도 라우팅한 적이 없다.**
⇒ `design_then_impl` 은 오늘 **이득 0, 런 하나가 둘이 되는 비용만** 남는다
(8월 live ws 28건 · 취소 15/32).

⚠️ 기록이 08-18 에 시작됐으므로 **그 이전은 이 근거로 말할 수 없다**
([[activity-recording-drift-invalidates-historical-counts]]).

### 6.3 ✅ intent 축은 **모양이 틀리지 않았다** — Q2 의 가설 3 기각

형님 문장으로 컴파일러 dry-run (`bsvibe_run_routing_rules_compile`):

> *"아키텍처 설계나 리팩터링 방향 잡는 일처럼 판단이 어려운 요청은 opus로, 나머지는 전부 sonnet으로"*

```
intent_name: architecture_design
intent_examples: 6개 (전부 한국어)
condition: classified_intent == architecture_design → opus
+ default → sonnet
```

**정확히 형님 니즈를, 형님 어휘로, 새 축 0개로.** 삭제된 키워드 폴백이 못 하던 것이다.

### 6.4 🔴 그런데 지금 적용하면 **조용한 no-op** — Q2 의 진짜 답

`build_intent_classifier` 는 `account_embedding_settings` 행이 없으면 `None` 을 리턴한다
(`EmbeddingSettings.from_account_settings`: *"No default model: accounts must opt in explicitly"*).

| | |
|---|---|
| prod `account_embedding_settings` | **0행** |
| `EmbeddingSettingsRepository.upsert` 호출자 | **유닛테스트 1개뿐** — REST·MCP·PWA 어디에도 쓰기 표면이 없다 |

그리고 `backend/api/v1/intents.py` 는 *"no embedding model configured → 그래도 만든다
(`embedding=None`)"* 라고 **명시적으로 관대하다.** ⇒ intent 는 만들어지고, 저장되고,
**영원히 매치 안 된다.**

∴ **Q2 의 답은 "발견성"도 "순환"도 "틀린 축"도 아니다 — 전원선이 없다.**
전형적인 [[config-menu-offers-options-nothing-implements]].

바로 옆 knowledge 경로는 **반대로** 갔다 — `backend/config.py:226` 이 사유까지 적어뒀다:
*"routing embedding config 와 달리 knowledge search 는 워크스페이스별 opt-in 이 아니다."*
`note_embeddings` 39행이 그 경로가 살아있음을 증명한다.

### 6.5 🐛 부수 발견 — 컴파일러가 "설계 작업"을 `workflow.frame` 으로 보낸다

*"설계처럼 깊이 생각해야 하는 작업은 opus, 구현 작업은 sonnet"* → 컴파일 결과:

```
caller_id: workflow.frame        → opus     ← frame 은 싸구려 분류 호출이다
caller_id: workflow.agent_loop.act → sonnet ← 진짜 작업은 전부 여기
```

**정확히 반대로 나온다.** caller 는 *호출 지점*이지 *작업의 종류*가 아닌데,
컴파일러의 "execution stage" 차원이 그 둘을 같은 것으로 취급한다.

### 6.6 현행 라우팅의 시간 해상도

- 해결은 **런-드라이브당 1회** (`agent_runtime._factory` → `resolve_via_caller`)
- 그 안에서 `_drive_loop: for _cycle in range(orch._max_cycles)` — **모든 턴이 같은 모델**
- `RoutingContext.from_run` 은 **프레임 시점 신호만** 읽는다 → 턴마다 다시 풀어도 답이 같다
- 룰의 `target` 은 `litellm_model` 문자열 하나 — **모델 계정만** 가리킨다
- `stage` 값에 대한 **고정 어휘 검증은 어디에도 없다** (자유 문자열)

---

## §7 ✅ 형님 확정 설계 — 룰에서 도출되는 동적 체이닝

> 형님 (2026-08-24):
> > *"줄곳 말하지만 이런 체이닝도 정적으로 하는게 아니라, 사용자의 라우팅에 따라
> > 동적으로 가능해야 해"*
> > *"사용자 별로 라우팅 룰들을 종합해서 먼저 통합 된 동적 체이닝 규칙을 만들어야 해.
> > 그러면 프레이밍 단계에서 그 규칙을 기반으로 작업을 쪼개고, 할당할 수 있겠지"*

### 7.1 파이프라인

```
사용자 룰 (자연어)  ──종합──▶  통합 체이닝 규칙 (도출, 저장 안 함)
                                      │
                              프레이밍 단계
                                      │
                          작업을 N개 스텝으로 쪼갬
                          각 스텝에 사용자 어휘의 stage 라벨
                                      │
                         기존 엔진이 stage 룰로 모델 할당
```

### 7.2 불변식

1. **어휘는 사용자 것이다.** `single`/`design_then_impl` 고정 2값은 사라진다.
   단계 라벨은 형님 룰에서 나온다.
2. **룰이 없으면 쪼갬도 없다.** 근거가 없으니 예측도 없다 — 한 런으로 간다.
   (오늘 live 워크스페이스가 정확히 이 경우다.)
3. **라우팅 권위는 하나.** 프레이머는 *라벨*만 붙이고, 모델은 **기존 엔진**이
   `stage == X` 룰로 고른다. 두 번째 소스를 만들지 않는다 (INV-7).
4. **새 축 0개.** `stage` 는 이미 `ALLOWED_FIELDS` 에 있고 자유 문자열이다.
   `pipeline` 만 빠진다.
5. **스텝의 지시문은 쪼갬이 만든다.** 하드코딩된 "spec only, don't build"
   (`design_directive_message`) 는 사라진다 — #770 이 만든 *"명세만 받고 완료 들음"*
   상태가 원리적으로 불가능해진다.

### 7.3 PR 분해 (순차, 하나씩)

| PR | 내용 | 상태 |
|---|---|---|
| **A** | `classified_intent` 전원선 — knowledge 임베딩 설정 재사용 | ✅ **#817 머지** (`3a4c61a`) |
| **B** | 축 교체 — 고정 예측 삭제 + 종합 + 프레이밍 쪼갬 + N단계 체이닝 + NL 컴파일러 | 작업 중 |

⚠️ **B 를 쪼개지 않은 이유.** 처음엔 B(삭제)·C(신규)·D(컴파일러) 셋으로 나눌
생각이었는데, 재보니 **삭제만 하는 PR 이 프로덕션에 반쪽 상태를 남긴다**:

* `pipeline` 만 지우면 체이닝이 통째로 사라진다 — #770 이 정확히 그 상태
  (*명세만 받고 완료 들음*)의 대가를 이미 쟀다.
* 컴파일러를 나중으로 미루면 형님이 단계 룰을 **만들 길이 없다** — 방금 PR A 에서
  고친 `classified_intent` 와 **같은 모양의 반쪽 배선**이다.
* handoff 기계는 재사용된다 (`design_*` → `prior_*`). 지웠다 다시 넣는 건 churn.

∴ B 는 **교체 하나**다. 추가가 아니라 축의 교체이므로 원자적이어야 한다.

### 7.5 ⚠️ prod E2E 로 증명할 수 있는 것과 없는 것

**증명 가능** — 형님 live 워크스페이스(룰 0개)에서 런이 **쪼개지지 않는다**.
이게 안전 경로고, 오늘 동작과 같아야 한다.

**증명 불가(내가 하면 안 됨)** — 쪼개지는 경로는 `stage` 룰이 있어야 보인다.
룰은 **형님 설정**이라 내가 만들면 [[feedback_user_config_is_not_a_defect]] 위반이다.
대신 admin 워크스페이스에 **형님이 이미 만들어 둔** `design`/`impl` 룰 2개가 있고
08-09 이후 유휴다 — 거기서 재면 내가 지어낸 설정이 아니다.

### 7.4 형님이 확정한 잔여물 처리

- **admin 워크스페이스 룰 3개 · prod payload 32건 → 둘 다 그대로 둔다.**
  룰은 사용자 설정이라 코드가 임의로 못 지운다 ([[feedback_user_config_is_not_a_defect]]).
  payload 는 과거 기록이고, 읽는 코드가 없어지면 무해한 잔여 필드가 된다. 마이그레이션 0개.

### 7.6 구현 중 스스로 잡은 것 둘

**(1) #690 위반 — 스텝 brief 가 형님 원문을 덮을 뻔했다.**
처음 구현은 쪼갠 각 런의 `intent_text` 를 그 스텝의 brief 로 **대체**했다. 그 brief 는
프레이머가 쓴 요약이므로, 형님이 쓴 요구사항 중 요약에 안 들어간 것은 **그 지점에서
영원히 사라진다.** #690 이 512자 truncation 으로 같은 손실을 이미 쟀다 — 에이전트는 받은
절반만 만들었고, lint/test 는 그 절반 위에서 통과했다.

처방: `intent_text` 는 체인 내내 **형님 원문**이고, 스텝의 brief 는 `step_intent` 로 따로
간다. `_intent_directive` 가 원문 뒤에 *"이 런은 그 요청의 한 스텝이다. 네 몫은 —"* 를
덧붙인다. 대체가 아니라 **범위 지정**.

**(2) 개수를 세는 양성 대조군은 자기가 무엇을 지키는지 모른다.**
`test_the_routing_fields_are_untouched` 가 `len(ALLOWED_FIELDS) >= 11` 이었다.
관할권 축 삭제와 무관함을 지키려던 대조군인데, **관할권과 아무 상관 없는** `pipeline`
삭제가 그걸 깨뜨렸다. 개수는 지키려는 것과 무관한 이유로 움직인다 → 필드 **이름**을 센다.

### 7.7 어휘는 순서가 아니다

prod 의 형님 stage 룰 둘은 `priority` 가 **같다**(둘 다 10). 그래서 tiebreak 없이는 DB 가
돌려준 행 순서가 그대로 프롬프트 순서가 되고, 같은 룰인데 런마다 다른 프롬프트가 된다.
게다가 그 목록이 모델에게 **순서**로 읽히면 안 된다 — 라우팅 priority 는 *어느 룰이
매치를 이기나*이지 *무엇을 먼저 하나*가 아니다.

처방: `(priority, name, id)` 로 결정적 정렬 + 프롬프트에 *"이건 집합이지 순서가 아니다,
순서는 네가 정한다"* 를 명시.

### 7.8 양끝은 덮여 있었는데 전선은 아무도 안 재고 있었다

PR 을 올린 뒤 커버리지를 되짚다 찾았다. `derive_stage_vocabulary` 유닛테스트 10개,
프레이밍 쪼갬 유닛테스트 13개 — 그런데 **그 사이 워커 경로는 테스트가 0개**였다:

```
룰 조회 → 어휘 생성 → FrameConfig 로 전달 → 돌아온 steps 를 payload 에 기록
```

이게 끊기면 payload 에 `stage` 가 없고 → `RoutingContext._derive_stage` 가 `"single"` 을
보고 → **형님 룰은 발화하지 못한 채 라우팅이 조용히 기본값으로 떨어진다.**
그리고 유닛테스트는 전부 green 이다.

이건 이번 세션이 prod 에서 발견한 것과 **똑같은 모양**이다(§6.1·§6.4). 내가 그걸 고치면서
같은 구멍을 새로 팔 뻔했다.

검증: `_stage_vocabulary_for` 가 `[]` 를 리턴하도록 **실제로 전선을 끊어** 양성 테스트가
깨지는 것을 확인했고, 음성 대조군(룰 0개)은 그대로 통과하는 것도 확인했다.

부수 확인: **스텝이 하나여도 라벨은 붙는다.** 형님의 실제 니즈가 거기 있다 —
*"설계 작업은 opus"* 는 런을 쪼개라는 뜻이 아니라 **그 일을 그 모델로 보내라**는 뜻이다.
쪼개기는 스텝이 둘 이상일 때만 일어난다.

→ [[wiring-guard-must-cut-the-wire-not-just-the-endpoint]] · [[seam-must-assert-what-the-consumer-sees]]

### 7.9 ⚠️ 내가 결함으로 오진할 뻔한 것 — 워커 `status='online'`

prod `executor_workers` 에 하트비트가 27일·34일·73일 지났는데 `status='online'` 인 행이
3개 있다. 형님께 *"화면이 거짓말한다"* 고 보고했다가 코드를 열어보고 **정정했다.**

이미 처리돼 있다. 코드 주석이 그대로 말한다 —
*"``status="online"`` can lie when the worker process died before clearing the column"* (Lift E13):

* **디스패치**(`find_available_worker`)는 `status=online` **AND** 하트비트 신선도(120s)를
  **둘 다** 요구한다. 핀 고정된 워커조차 하트비트가 낡으면 핀을 버리고 살아있는 워커로 간다
* **REST·PWA 목록**은 `heartbeat_fresh` 를 별도 필드로 내보낸다

**교훈**: 한 컬럼만 보고 "표면이 거짓을 말한다"고 판정하지 마라. 그 값을 **읽는 쪽**이
무엇을 함께 보는지 먼저 세라. 기준은 *시스템이 자기 계약을 어겼나* 이지
*이 컬럼이 그 자체로 정확한가* 가 아니다. → [[feedback_user_config_is_not_a_defect]]

---

## §8 배포 실증 (2026-08-24, prod `913ca92`)

### 8.1 안전 경로 — 증명됨

런 `17539a76` → `review_ready` · **딜리버러블 1건** ·
`frame.pipeline` **없음** · `frame.steps` **없음** · `payload.stage` **없음**.

배포된 코드로 종합을 직접 돌린 결과 (컨테이너 내 읽기 전용 probe):

```
live (qazasa123): 룰 0개 → 어휘 0개      ← 절대 안 쪼갬
admin:            룰 3개 → design · impl  ← 형님이 쓰신 룰 그대로
```

### 8.2 🔴 그 스모크가 무관한 프로덕션 결함을 잡았다

첫 스모크 `dbcd8115` 는 **`failed` · 딜리버러블 0** 이었다. `status` 만 봤으면 넘어갔다.

```
aborted: the CLI's tools are not the ones BSVibe sanctioned
(unsanctioned: ListAgents, ReportFindings, SendMessage)
```

#814 가 **오늘** 고친 결함인데 다시 났다. 원인은 코드가 아니라 **실행 표면 개수**였다 —
파운더 머신에 워커 데몬이 **셋**이고 재시작된 건 하나뿐이었다. `com.bsvibe.worker`
(08-11 시작, 13일 된 코드)가 live 워크스페이스 태스크를 집어가 abort 시켰다.

∴ 에이전트 런의 성패가 **어느 데몬이 먼저 폴링하느냐**로 갈리고 있었다. 08:03Z 이전
세션 E2E 는 성공(새 데몬), 11:07Z 내 스모크는 실패(낡은 데몬). 같은 날, 같은 코드.

조치: 유휴 확인 후 `launchctl kickstart -k` 로 낡은 데몬 둘 재시작 → 재검증 통과.

**교훈**: *"머지했다 → 배포됐다 → 고쳐졌다"* 사슬이 **실행 표면 개수**를 가정한다.
낡은 데몬도 하트비트는 정상이고 `status='online'` 이라 아무것도 알려주지 않는다.
낡음의 유일한 신호는 **프로세스 시작 시각**이다.
→ [[one-machine-runs-several-worker-daemons]]

### 8.3 미증명으로 남는 것

**쪼개지는 런 전체.** admin 워크스페이스(형님 룰 + 살아있는 워커)에서 런을 넣어야
하는데 내 MCP 토큰이 live 워크스페이스에 묶여 있다. 단위·통합 테스트로는 전부 덮여
있고, 워커 seam 은 **전선을 실제로 끊어** 테스트가 진짜임을 확인했다(§7.8).

---

## §9 쪼개지는 경로 실증 (2026-08-25, 형님 지시 *"mcp로 세팅해서 실증 돌리자"*)

§8.3 이 미증명으로 남겼던 것을 형님 지시로 닫았다. 룰은 **MCP 로 형님 워크스페이스에
직접 만들었다** (`bsvibe_run_routing_rules_compile` → `_apply`).

### 9.1 §6.5 오매핑 수정 — 같은 문장, 다른 결과

> *"설계처럼 깊이 생각해야 하는 작업은 opus로 보내고, 구현 작업은 sonnet으로"*

| | 배포 전 (2026-08-24 실측) | 배포 후 |
|---|---|---|
| "설계 작업" | `caller_id: workflow.frame` 🔴 **싸구려 분류 호출**에 opus | `stage == "design"` ✅ |
| "구현 작업" | `caller_id: workflow.agent_loop.act` | `stage == "implement"` ✅ |

### 9.2 전 사슬 실증 — 런 `e6472fab`

```
룰 2개 (형님이 MCP 로 생성)
  → 종합:   design(설계 단계는 opus로) · implement(구현 단계는 sonnet으로)
  → 프레이밍: steps 2개로 쪼갬
       [0] stage=design    "…주입 가능한 seam 구조를 설계하고 …판별 로직을 설계한다"
       [1] stage=implement "설계에 따라 …구현하고 …테스트를 포함한다"
  → payload: stage=design · step_index=0
  → 라우팅:  explicit_rule → opus       ← prod 기록상 최초
```

**`explicit_rule` 은 prod 에서 처음 나왔다.** 그 전 21건은 전부
`workspace_default → sonnet` 이었다(§6.2). 형님이 2026-06-28 에 만드신
*"설계는 opus"* 라는 의도가 **오늘 처음으로 실제 런을 라우팅했다.**

#690 도 함께 확인: `intent_text` 는 형님 원문 그대로, 스텝의 brief 는 `step_intent` 로 분리.

### 9.3 남은 관문

첫 스텝 종료 후 `step_index=1` 런이 spawn 되는가 (`prior_run_id` · `prior_output_text`).

### 9.4 산출물 처리 (형님 지시 *"결과물은 판단해서 처리해줘"* · *"#819는 판단해서 처리해"*)

배송 경로가 없었다 — 이 제품에 **바인딩 0개**. 형님 지시로 먼저 살렸다:
활성 GitHub 커넥터의 `delivery_config` 가 비어 있어 `repo: BSVibe/bsvibe-app` 를
넣고, 제품↔커넥터 바인딩을 `output_mode: safe` 로 만들었다. 그 다음 최종
산출물만 승인 → **PR #819**. 나머지 6건은 사유를 달아 거절(중복 중간 산출물).

**부수 성과**: 그 거절 6건이 인수인계에 세 세션 연속 미결이던 **#782 · #784** 를
함께 닫았다 — 거절된 두 런이 `review_ready → open` 으로 복귀했고(#782),
`note_embeddings.idx_scan` 이 40 → 43 으로 올랐고(#784), settle 활동 7건이 났다.
문서가 확정한 트리거(**거절 + 사유**)가 맞았다. 지어낸 거절이 아니라 진짜 사유였다.

**PR #819 판정 — 머지**. 실측으로:
* CLI 를 실제로 돌려 stale 데몬 3개 지목, **exit 1**(워치독 사용 가능),
  git 아닌 경로는 `ProbeError` 로 정직하게 실패
* 테스트 48개, 실제 launchctl/ps/git 호출 **0회**
* `test_the_pure_module_imports_nothing_that_can_spawn_a_process` —
  순수 모듈이 프로세스를 못 띄우는 것을 **테스트가 구조적으로 강제**
* 로케일(`LC_ALL=C` 핀 + 비영어 월 거부) · naive datetime 거부 · 사라진 PID ·
  `com.bsvibeer` 오매칭 배제 전부 방어
* 기존 파일 오염 **0** (`test_cli.py` 가 main 과 바이트 동일로 복구)

### 9.5 🔴 다음에 볼 갭 — 선언한 검사와 레포 CI 가 어긋난다

#819 의 CI 를 막은 것은 **`ruff format --check` 3파일**이었다. 에이전트의 검증
선언에는 `pytest` 와 `ruff check` 는 있었지만 **`ruff format --check` 가 없었다.**
그래서 **자기 게이트는 통과하고 CI 가 떨어졌다.**

`derived_gate` 가 바로 그 간극(레포가 실제로 요구하는 검사)을 메우라고 있는
장치인데 이번엔 못 메웠다. 같은 런에서 `gate_deriver_failed` 가 났던 것과
무관하지 않을 수 있다 — **deriver 가 못 돌면 레포의 진짜 게이트가 통째로
빠진다**(#820 이 그 사실을 이제 에이전트에게 알려준다).

측정할 것: 머지된 PR 중 CI 가 `format`/`mypy`/`lint-imports` 로 떨어진 비율 vs
그 런들의 `derived_gate` 유무.

---

# 파트 3 — §9.5 갭 실측 + 처방 (2026-08-25 세션)

## §10 측정 — deriver 는 레포의 진짜 게이트를 **본 적이 없다**

### 10.1 prod 실측 (`bsvibe-prod-postgres-1`, 2026-08-25)

`verification_results` 408행 중 `derived_gate` 있는 것 360행. 제품별로 갈라서 셌다
(규율 7 — 인수인계 숫자 인용 금지):

| 제품 | 게이트 | 명령 있음 | `ruff check` | `ruff format --check` | `mypy` | `lint-imports` |
|---|---|---|---|---|---|---|
| **BSVibe** | 142 | 94 | **90 (96%)** | **42 (45%)** | 72 (77%) | **15 (16%)** |
| Toolkit | 114 | 99 | 99 | 3 | 0 | 0 |
| BStockReport | 99 | 38 | 34 | 26 | 0 | 0 |

BSVibe CI(`.github/workflows/ci.yml`)는 이 넷을 **전부** 요구한다. 그런데 게이트가
그 넷을 담는 비율은 96% / 45% / 77% / 16% 로 갈린다.

### 10.2 #819 를 떨어뜨린 게이트를 직접 열어봤다

최종 검증(run `3bd1f8b8`, `outcome=passed`)의 명령 5개:

```
ruff check … | mypy … | python -m pytest … | lint-imports | (제약 grep)
```

**`ruff format --check` 없음.** CI 를 떨어뜨린 것이 정확히 그것이다.
같은 트랙의 이전 런 `e6472fab` 은 같은 파일에 대해 `ruff format --check` 를
**포함했다.** ⇒ 결함은 "못 한다"가 아니라 **매번 다르게 찍는다**(45%).

### 10.3 왜 찍는가 — 근거가 프롬프트에 안 들어간다

`verification_service._MANIFEST_FILES` (10개):

```
pyproject.toml setup.cfg setup.py package.json Cargo.toml
go.mod Makefile justfile pom.xml build.gradle
```

레포에서 검사를 **문자 그대로** 선언하는 파일들:

| 파일 | 담긴 것 | deriver 에게 보이나 |
|---|---|---|
| `.github/workflows/ci.yml` | `ruff format --check …` · `lint-imports` · `mypy backend/` **verbatim** | ❌ |
| `ruff.toml` (7.6KB) | ruff 설정 SoT | ❌ |
| `mypy.ini` | mypy strict 설정 | ❌ |
| `pyproject.toml` | `ruff>=0.6` · `mypy>=1.11` · `[tool.importlinter]` | ✅ |

그런데 `_DERIVATION_SYSTEM_PROMPT` 는 이렇게 지시한다:

> "use only tools, runners, flags, and extras that appear in the provided
> manifests / build config / **CI**"
> "Prefer a command the repo declares VERBATIM (a build-tool target, a package
> script, **a CI step**)."

**따를 수 없는 지시다.** CI 를 한 번도 안 보여준다. 그래서 pyproject 에서
*추론 가능한* 것(`ruff` 존재 → `ruff check`, `mypy` 의존성 → `mypy`)은 높고,
**CI 에만 이름이 있는 것**(`format --check` 서브커맨드 · `lint-imports` 명령명)은
낮다. 96/77 vs 45/16 이 정확히 그 모양이다.

→ 스킬 후보 `prompt-tells-the-model-to-ground-on-what-it-never-sees`

### 10.4 처방 — CI 선언을 읽어서 deriver 에게 준다

**하지 않을 것**: 스택별 검출기 목록(`if pyproject: run ruff format`). 이 모듈의
설계 전제가 *"레포가 스스로 선언한 것에 근거한다"* 이고, 하드코딩은 그걸 되돌린다.

**할 것**: 레포의 CI 선언 파일을 읽어 프롬프트에 **별도 블록**으로 넣는다.
프롬프트는 이미 CI 를 근거로 쓰라고 말한다 — 이제 실제로 줄 뿐이다.

⚠️ **`_read_repo_manifests` 는 건드리지 않는다.** 그 반환값은 두 곳에서
*fail-closed 판정*을 겸한다:
- `_manifest_present()` → "툴체인 있음 ⇒ 게이트 기대됨 ⇒ deriver 실패 시 fail-CLOSED"
- `inplace_gate` L242~244 → `if not manifests: return None` (게이트 없음 = 정직한 무게이트)

여기에 CI 파일을 섞으면 **CI 만 있는 문서 레포가 "툴체인 있음"이 된다** — 다른
질문에 답하게 된다. 그래서 **읽는 함수를 새로 만든다**(`_read_ci_declarations`).

### 10.5 불변식

1. `_read_repo_manifests` 의 반환 집합 **불변** (양성 대조군으로 고정)
2. deriver 프롬프트는 **스택 불가지** 유지 (툴 이름 금지 — 기존 테스트가 지킴)
3. CI 블록은 **바이트 상한** (파일 수 · 파일당 바이트 · 총량)
4. CI 디렉터리 부재/읽기 실패 = 조용히 빈 결과, **예외 없음**
5. 전선을 실제로 재는 테스트 — 양끝 유닛이 아니라 **CI 텍스트가 프롬프트에
   도달하는가** (규율 2: 주는 쪽·받는 쪽 양쪽에서 끊어보기)

### 10.6 측정 재실행 (머지 후)

같은 쿼리로 BSVibe 게이트의 `format`/`lint-imports` 포함률을 다시 센다.
⚠️ 배포 직후 0 은 *새 런이 0건*이라서일 수 있다 — 스모크 런을 실제로 넣고 세라
(§8 에서 이미 한 번 물렸다).

### 10.7 나머지 절반 — 에이전트 브랜치의 CI 실패는 **한 검사에 몰려 있다**

§9.5 가 요구한 측정("머지 PR 중 CI 가 format/mypy/lint-imports 로 떨어진 비율").
GitHub Actions 에서 직접 셌다 — `bsvibe/run-*` 브랜치(= 에이전트가 만든 PR) 전수.

⚠️ 먼저 **검출기를 양성 대조군으로 검증**했다. PR 최종 head sha 의 check-run 을
세면 **전부 success** 다(머지 전에 고쳐졌으니까). 그 0 을 그대로 믿었으면
"CI 실패 없음"이라고 보고할 뻔했다. 실패는 **브랜치의 이전 커밋**에 있다 —
`gh run list --branch` 로 워크플로 런 전체를 봐야 보인다.

| 워크플로 런 | 브랜치 | 떨어진 스텝 |
|---|---|---|
| 32801336342 (08-25) | `run-389ddb8a` (#819) | **Ruff (format check)** |
| 32004334624 (08-17) | `run-e3c08708` | pytest (진짜 테스트 실패) |
| 31858989887 (08-15) | `run-e53e9b5c` | **Ruff (format check)** |
| 31858982431 (08-15) | `run-e53e9b5c` | **Ruff (format check)** |

**실패 4건 중 3건(75%)이 `ruff format --check`.** `mypy` 로 떨어진 것 **0건**,
`lint-imports` 로 떨어진 것 **0건**. 나머지 1건은 정당한 테스트 실패다.

n=4 로 작다. 하지만 **집중도는 완전하다** — 에이전트 PR 을 막은 비(非)테스트
실패는 **전부** 같은 검사 하나였다. §10.1 의 포함률(format 45%)과 방향이 정확히
맞물린다: 게이트가 절반만 담는 검사가, 실제로 막는 검사의 전부다.

`lint-imports` 는 포함률 16% 로 가장 낮은데 **한 번도 CI 를 못 떨어뜨렸다** —
에이전트가 import 계약을 어길 일이 드물어서지, 게이트가 잘해서가 아니다.
⇒ 포함률만으로 위험을 정렬하지 마라. **막은 적 있는가**를 함께 세라.

---

## §11 #822 머지 + prod 실증 (2026-08-25, `da201d1`)

머지 `da201d1` → autodeploy **80초** 만에 prod 반영. 수동 배포 없음.

### 11.1 결정론적 절반 — 배선 (배포된 코드 · 실제 레포)

읽기 전용 probe. 배포된 `_read_ci_declarations` 를 실제 `bsvibe-app` 트리에 물림:

```
CI files found: ['.github/workflows/ci.yml']   (3983 chars)
CI 가 실제로 게이트하는 명령 4개가 프롬프트에 verbatim 도달하는가:
  OK  uv run ruff check backend/ tests/ bsvibe_sdk/ plugin/
  OK  uv run ruff format --check backend/ tests/ bsvibe_sdk/ plugin/
  OK  uv run lint-imports
  OK  uv run mypy backend/
CI block header present: True
```

배포 전에는 이 넷 중 **0개**가 프롬프트에 있었다.

### 11.2 행동 절반 — A/B, 새 PR 을 만들지 않고

§10.6 은 "스모크 런을 넣고 세라"고 적었지만, 더 나은 방법이 있었다: **과거 런의
실제 입력을 그대로 쓰고 CI 블록만 켰다 껐다** 한다. 새 작업을 발주하지 않고,
같은 입력·같은 모델·같은 프롬프트에서 **한 변수만** 다르다.

입력 = prod 런 `3bd1f8b8` (= PR #819, CI 가 format 으로 떨어진 그 런)의 원문 intent
+ 변경 파일 6개 + pyproject + baseline. 모델 = `workspace_default → sonnet`
(qazasa123 ws, executor provider). 각 조건 **n=8** (4회 × 2라운드, 2라운드가
1라운드를 정확히 재현).

| | `ruff format --check` | `lint-imports` | `mypy` | `ruff check` |
|---|---|---|---|---|
| **CI 없이** | **0 / 8** | 6 / 8 | 8 / 8 | 8 / 8 |
| **CI 주고** | **8 / 8** | 7 / 8 | 8 / 8 | 8 / 8 |

`ruff format --check` **0/8 → 8/8**. 완전 분리 (Fisher exact p ≈ 7.8e-5).
그리고 그것이 §10.7 에서 에이전트 PR 을 막은 비테스트 CI 실패 **4건 중 3건**이다.

### 11.3 ⚠️ 가설이 절반만 맞았다 — `lint-imports`

§10.3 에서 나는 *"CI 에만 이름이 있는 것 둘이 낮다"* 며 format(45%)과
lint-imports(16%)를 **같은 원인**으로 묶었다. A/B 는 그걸 **기각**한다:
CI 없이도 `lint-imports` 가 6/8 로 이미 나온다. pyproject 의 `[tool.importlinter]`
만으로 충분히 추론되고 있었다.

⇒ 역사적 16% 는 **CI 비가시성으로 설명되지 않는다.** 다른 원인이거나(그 시기
intent/파일 맥락이 달랐거나) 그냥 개선된 것이다. **아직 모른다.**

교훈: 같은 방향으로 움직인 두 숫자를 하나의 원인으로 묶고 싶은 유혹이 강하다.
A/B 는 하나만 통과시켰다. **묶은 가설은 변수를 하나씩 끊어서만 확인된다.**

### 11.4 상태

* prod `da201d1` · 열린 PR 0 · 워크트리 0 · 테스트 컨테이너 0
* probe 파일은 prod 컨테이너에서 삭제 확인 (`/tmp/ab_probe.py`, `/tmp/ab_inputs.json`)
* §10.6 의 "머지 후 포함률 재측정"은 **A/B 가 더 강한 증거로 대체**했다.
  자연 발생 런으로도 세려면 새 런이 쌓인 뒤 §10.1 쿼리를 다시 돌리면 된다
  (기준선: 2026-08-25 02:56:11+00 이전 = 배포 전).

## §12 대기 승인 5건 처리 + **두 세션 미실증 주장의 실증** (2026-08-25)

형님 지시 *"대기 중인거는 확인해서 처리해줘"*.

### 12.1 5건의 정체 — 전부 어제 검증 트랙의 잔여물

| 항목 | 런 | 타입 | 판정 근거 |
|---|---|---|---|
| `ad071579` | `e6472fab` | direct_output | **자기 요약이 스스로 말한다**: *"이 스텝의 산출물은 별도 PR 로 배송하지 않고 구현 스텝(PR #819)에 포함된 것으로 본다"* |
| `46cbba78` | `e6472fab` | code (35.8KB diff) | 같은 도구의 **이전 시도**(4파일). #819 로 대체됨 |
| `e8e41004` | `17539a76` | code (**diff 없음**) | 조사형 질문("라우팅 규칙 개수 확인")의 답변. 배송할 코드가 없음 |
| `74aea7b8` | `3bd1f8b8` | direct_output | 같은 도구의 요약 |
| `d99f686c` | `3bd1f8b8` | code (44.4KB diff) | **다른 변종**(7파일, README·test_doctor_cli 추가). 실제 머지된 #819 는 `389ddb8a` 의 6파일 변종 |

⇒ 5건 모두 **이미 머지된 `3fa0234`(#819)로 대체**되었거나 배송할 것이 없다.
승인했다면 중복·충돌 코드가 실제 PR 로 나갔을 것이다.

### 12.2 처리 — 런 단위 · 빈 사유

`bsvibe_safe_mode_deny_run` × 3 (item 단위 아님 — #769 의 교훈: *"Safe Mode 는
per-run 트랜잭션이라 항목 단위로 거절하면 나머지가 영원히 pending"*).
사유는 **비움** — 이건 판단이 아니라 정리이고, 빈 사유는 가르침 신호가 아니다.

### 12.3 ✅ 두 세션 연속 미실증이던 주장을 여기서 실증했다

`safe_mode_queue.py:167` 의 `if flipped and reason_text:` — *"사유 없는 정리는
settle 을 안 일으킨다"* 는 #782·#784 에서 **두 세션 연속 미실증**으로 남아 있었다.
기준선을 먼저 고정하고 처리 후 다시 쟀다:

| | 처리 전 | 처리 후 |
|---|---|---|
| pending | 5 | **0** |
| denied (null reason) | 146 | **151** (+5) |
| denied (with reason) | 17 | **17** (변동 없음) |
| `settle` 활동 | 143 | **143** ← 지식화 미발화 |
| 전체 활동 | 4547 | **4547** ← 활동 행 자체가 0개 |
| `note_embeddings` | 1723 | **1723** ← 지식 오염 0 |
| 런 상태 3개 | review_ready | **review_ready** ← 런 재개 안 됨 |
| delivery_events(1h) | — | **0** · 열린 PR **0** · `bsvibe/*` 브랜치 **0** |

**전부 확인.** 빈 사유 거절은 순수 상태 전이다 — 지식화도, 런 재개도, 배송도 없다.
(사유를 **주면** 둘 다 발화한다: `_record_rejection_knowledge` + `_reopen_run_with_the_reason`.)

## §13 🔴 원클릭 승인이 지식이 된다 — 근본 원인 (2026-08-25)

§12 의 Decision 을 `acknowledge` 로 종결했더니 **vault 노트가 하나 생겼다.**
내가 형님께 "조용히 종결"이라고 설명한 것과 다르다. **내가 세운 기준선 측정이
내 설명을 반증했다** (`settle` 143→144 · `note_embeddings` 1723→1724).

### 13.1 근본 원인 — 규칙의 전제를 생산자 하나만 지킨다

`worth_remembering.is_inherently_notable(kind)` 는 두 kind 에 *"LLM 판단 없이 무조건
기억가치 있음"* 을 부여한다. 그 **자기 docstring 이 전제를 명시**한다:

> "a user decision **or a discard-with-reason** is knowledge by construction"

즉 *형님이 실제로 무언가를 썼다*가 전제다. 그런데 그 규칙을 먹이는 생산자는 둘이고,
**전제를 지키는 쪽은 하나뿐이다**:

| 생산자 | kind | 전제 강제? |
|---|---|---|
| Safe Mode `deny` | `negative_pattern` | ✅ `if flipped and reason_text:` (`safe_mode_queue.py:167`) |
| `resolve_checkpoint` | `decision_resolution` | ❌ **무조건 기록** |

같은 규칙, 두 생산자, 한쪽만 준수. → [[mirrored-surface-drifts-in-the-direction-of-least-testing]]

그리고 불변식은 이미 코드에 글로 적혀 있다 (`_record_rejection_knowledge`):

> "매칭 근거는 **형님이 직접 쓴 텍스트**뿐이다 — reason + 런의 intent_text.
> 딜리버러블 요약은 LLM 생성물이라 쓰지 않는다"

**원클릭 액션은 형님이 쓴 글자가 0자다.** `answer` 필드에 버튼 키(`acknowledge`)가
그대로 들어가고, 노트 본문은 **시스템이 자기 질문을 되읊은 것**이 된다.

### 13.2 실측 — prod 전수

`decision_resolution` settle 활동 **11건 → vault 노트 11건** (1:1, 전부 생성됨).

| `action_key` | 건수 | `answer` 가 버튼 키 그대로 |
|---|---|---|
| (자유 텍스트) | 5 | 0 — **정당함** |
| `acknowledge` | 4 | **4** |
| `discard` | 2 | **2** |

**11건 중 6건이 형님이 쓴 글자 0자.** `discard` 2건도 주목 — docstring 은
*"discard-**with-reason**"* 이라 했는데 사유 없는 `discard` 도 그냥 통과한다.

🔴 **정정 (같은 날, 철회 작업 중 발견)** — 위 11건은 **워크스페이스가 섞인 숫자**다.
`group by workspace_id` 로 다시 세면 admin ws 2건(`truncate_middle`, 자유 텍스트) +
**qazasa123 ws 9건**이다. 형님이 실제로 일하시는 곳 기준으로는 **9건 중 6건(67%)**.

이건 이번 세션 §Ⅱ.1 에서 *배운* 바로 그 함정이다 — *"멀티테넌트에서 설정을
`count(*)` 로 세지 마라. `group by workspace_id` 로 세라."* 인수인계에서 읽고
같은 날 반복했다. 잡힌 계기는 숫자가 아니라 **철회 후 노트가 검색에 안 나온 것**이다.
→ [[config-lives-in-the-wrong-workspace]]

내가 만든 1건은 철회 완료(`ontology_corrections.applied_at=07:50:54`, 검색에서 소멸).
**남은 5건은 기존 것**(acknowledge 3 @08-18 · discard 2 @08-19).

### 13.3 고칠 것 — 예외 추가가 아니라 전제의 공유

`acknowledge` 만 예외 처리하면 `discard`(사유 없음)가 남고, 다음에 추가될 원클릭
액션이 또 샌다. 고쳐야 할 것은 **전제가 한 곳에만 있다는 사실**이다.

**불변식**: *settlement 은 형님이 직접 쓴 텍스트를 담을 때만 지식이 된다.*
버튼 키는 형님이 쓴 텍스트가 아니다.

⚠️ **깨뜨리면 안 되는 것** (양성 대조군):
1. 자유 텍스트 Decision 해소는 **계속** 노트가 된다 (위 5건)
2. 사유 있는 Safe Mode 거절은 **계속** `negative_pattern` 지식이 된다
   — 이게 trust ratchet 의 심장이다(#759/#760). 과교정으로 이걸 죽이면 안 된다

### 13.4 🔎 부수 발견 — *"지침 주고 다시 시도"* 버튼에 지침이 갈 길이 없다

§13 수정의 seam 을 검증하다(=`retry` 처럼 액션+텍스트가 함께 오는 경우 과교정 위험이
있는지) 발견. **PR #823 과 무관한 선재 결함이고, #823 을 막지 않는다.**

`checkpoint_resolution.py:210`:

```python
resolution_text = action_key if action_key is not None else answer
```

액션 키가 주어지면 형님이 친 `answer` 는 **그 자리에서 버려진다** — 그 뒤로는
어디에도 안 남는다:

* `decision.resolution` ← 액션 키
* `payload["resolved_decisions"]` ← 액션 키 (**재개하는 에이전트가 읽는 바로 그 값**)
* settle payload `answer` ← 액션 키

그리고 `reason` 은 **`action_key == ACTION_DISCARD` 분기에서만** 소비된다(L281).

그런데 `_checkpoint_shared.py:146` 의 액션 라벨은:

```python
DecisionAction(key=ACTION_RETRY, label_en="Guide & retry", label_ko="지침 주고 다시 시도")
```

**버튼 이름이 지침을 약속하는데 지침이 흐를 관이 없다.** 이번 세션이 계속 만난
모양 그대로다 — 표면이 약속하고 배선이 안 나른다.
→ [[config-menu-offers-options-nothing-implements]] · [[build-the-missing-link-not-the-missing-system]]

⚠️ **prod 실측: `retry` 사용 0회** (`action_key` 사용 6건 전부 acknowledge/discard).
즉 **아직 물린 적은 없다** — 코드 독해로만 확인된 것이고, 실증되지 않았다.
그러니 "결함 확정"이 아니라 **미실증 후보**로 남긴다. (n=0 을 실증으로 쓰지 마라.)

#823 이 이걸 악화시키지 않는 이유: L210 이 *"액션 키가 있으면 형님 텍스트는 이미
없다"*를 **코드로 보증**하므로, `founder_authored_text` 가 `action_key` 유무로
판정하는 것은 휴리스틱이 아니라 그 보증을 그대로 읽는 것이다.

### 13.5 ✅ #823 머지 + prod 실증 (`90d82b4`)

머지 `90d82b4` → autodeploy 반영. 검증은 **prod 의 실제 settle payload 전수를
배포된 판정 함수에 통과**시켰다 — LLM 없음, 결정론적이라 각 모양당 n=1 로 충분하다.
(새 Decision 을 만들어 기다릴 필요가 없다: 판정은 순수 함수다.)

| kind | `action_key` | 판정 | 건수 |
|---|---|---|---|
| `decision_resolution` | (자유 텍스트) | **KEEP** | 5 |
| `decision_resolution` | `acknowledge` | **SUPPRESS** | 4 |
| `decision_resolution` | `discard`(사유 없음) | **SUPPRESS** | 2 |
| `negative_pattern` | (자유 텍스트) | **KEEP** | **19** |

전수 30건 → 노트 24 · 억제 6. **억제 6건은 §13.2 에서 지목하고 철회한 바로 그 6건**이다.

**양성 대조군 둘 다 prod 데이터로 확인**:
① 형님이 직접 쓰신 Decision 해소 5건 전부 살아남는다
② **`negative_pattern` 19건 전부 살아남는다** — trust ratchet(#759/#760)의 심장이
   과교정으로 죽지 않았다. 이게 이 PR 에서 가장 중요한 숫자다.

probe 는 읽기 전용 확인(`note_embeddings` 1724 무변동) 후 컨테이너에서 삭제.

---

## §14 *"지침 주고 다시 시도"* 배선 잇기 — 형님 확정 A (2026-08-25)

### 14.1 🔴 §13.4 정정 — 지침이 사라지는 첫 지점은 백엔드가 아니라 **PWA** 다

§13.4 에 나는 `checkpoint_resolution.py:210` 이 범인이라고 적었다. **틀렸다.**
사슬을 끝까지 따라가니 텍스트는 **요청에 실리지도 않는다**:

```js
// CheckpointRow.tsx:86 — 액션 버튼
async function submitAction(actionKey: string) {
  await resolveCheckpointAction(item.checkpointId, actionKey);
}
// checkpoints.ts:41
body: JSON.stringify({ action_key: actionKey })      // answer 없음 · reason 없음
```

PWA 는 액션 버튼과 자유 텍스트를 **상호배타 경로 둘**로 만들었다. 버튼을 누르면
`answer` state 의 내용은 **클라이언트에서 버려진다.** L210 은 두 번째 방어선일 뿐
애초에 도달할 텍스트가 없다. → [[build-the-missing-link-not-the-missing-system]]

**같은 레포 안 네 곳이 서로 모순한다:**

| 위치 | 말하는 것 |
|---|---|
| `_checkpoint_shared.py:146` | `retry` 는 *"re-opens the run **with the founder's guidance**"* |
| 버튼 라벨 | **"지침 주고 다시 시도"** / "Guide & retry" |
| `checkpoints.py:124` | `reason` 은 *"**ignored for non-discard** resolutions"* |
| `CheckpointRow.tsx:86` | 버튼은 `action_key` 만 전송 |

실제 결과 — 재개된 에이전트가 받는 메시지:

> "The founder resolved a prior question — Q: … **A: retry**. Continue the work with this decision."

지침 자리에 문자열 `retry` 가 들어간다.

### 14.2 표면별 도달성 (prod 실측)

| 표면 | `action_key` | `answer` | `reason` |
|---|---|---|---|
| PWA 액션 버튼 | ✅ | ❌ 안 보냄 | ❌ 안 보냄 |
| PWA "Other" 자유 입력 | ❌ | ✅ | ❌ |
| MCP / REST | ✅ | ✅ | ✅ |

`negative_pattern` 19건의 출처: **safe mode deny 17 · checkpoint discard 2**.
즉 discard+reason 은 **MCP/REST 로는 도달하고 PWA 로는 도달 못 한다.**
`retry` 는 prod 사용 **0회** — 아직 아무도 안 물렸다.

### 14.3 고칠 것

액션 버튼이 **선택적 자유 텍스트를 함께** 보내고, 백엔드가 그것을 **지침으로** 실어
런을 재개한다. 없는 기계를 만드는 게 아니다 — 자유 텍스트 재개 경로는 이미 정상
동작한다. **링크 하나**다.

⚠️ **불변식**
1. `decision.resolution` 은 계속 **액션 키**(로케일 독립 — L206~209 의 이유 그대로).
   바뀌는 것은 *재개 메시지가 무엇을 나르는가*지 *무엇이 기록되는가*가 아니다.
2. **#823 과의 상호작용** — `founder_authored_text(answer, reason, action_key)` 는
   `action_key` 가 있으면 `reason` 이 비어 있는 한 **None** 을 낸다. 지침 있는 retry 를
   그냥 흘리면 **#823 이 그 지침을 지식에서 조용히 억제**한다. 지침은
   `founder_authored_text` 가 보는 자리로 넣어야 한다.
   ⇒ 오늘 머지한 게이트가 내일의 정당한 지식을 죽이는 것을 막는 것이 이 PR 의 절반이다.
3. 텍스트 없는 액션(`acknowledge`·사유 없는 `discard`)은 **계속 억제**된다(#823 유지).

### 14.4 ✅ #824 머지 + 실증한 것 / 못 한 것 (prod `e3b29b4`)

머지 `e3b29b4` → 백엔드 autodeploy 60초 · PWA Vercel Production 배포 `success`
(deployment sha=`e3b29b4`, 09:54:12Z).

#### 실증됨 — #823 × #824 상호작용 (배포된 코드, 순수 술어, 읽기 전용)

이 PR 의 절반은 *"오늘 만든 게이트가 내일의 정당한 지식을 죽이지 않는가"* 였다.
배포된 `founder_authored_text` + `is_inherently_notable` 에 7가지 모양을 통과시켰다:

| 케이스 | 형님 텍스트 | 노트 | 기대 |
|---|---|---|---|
| **지침 있는 retry (신규)** | YES | **True** | ✅ |
| 맨 retry | none | False | ✅ |
| 맨 acknowledge | none | False | ✅ |
| 맨 discard | none | False | ✅ |
| 지침 있는 discard 의 `decision_resolution` 행 | none | False | ✅ (한 사건 한 노트) |
| 자유 텍스트 답변 | YES | True | ✅ |
| `negative_pattern` (deny+사유) | YES | True | ✅ |

**7/7 설계대로.** 특히 1행 — 지침 있는 retry 가 지식이 된다. #823 이 그걸 안 죽인다.

#### ⚠️ 실증 못 함 — PWA→prod 왕복

`retry` 는 prod 사용 **0회**다. 증명하려면 실제 `merge_conflict_review` Decision 이
생기고 형님이 버튼을 눌러야 한다. **테스트 스위트 + 실 ASGI 클라이언트까지만**
확인됐고, 배포된 프론트에서 누른 적은 없다.

정직하게 남긴다: 이 PR 은 오늘 4건 중 **prod 실증이 절반뿐인 유일한 건**이다.
n=0 을 실증으로 쓰지 않는다. 형님이 다음에 충돌 리뷰를 만나 그 버튼을 쓰실 때
`resolved_decisions[].answer` 가 `retry` 가 아니라 형님 문장인지 확인하면 닫힌다.

확인 쿼리:
```sql
select payload->'resolved_decisions' from execution_runs
where payload::jsonb ? 'resolved_decisions' order by created_at desc limit 3;
```

---

# 파트 4 — 낡은 데몬 실측 + `lint-imports` 구조 규명 (2026-08-26 세션)

## §15 `bsvibe-worker staleness` 첫 사용 — 인수인계가 틀렸다

§Ⅳ.3 이 "#819 가 만든 도구를 아직 아무도 안 써봤다"고 남긴 것을 실제로 돌렸다.

**정정 1 — 명령 이름.** 인수인계는 `bsvibe-worker doctor` 라고 적었다. 실제 CLI 는
`register / run / status / logout / claude-login / service / staleness` 이고
**`doctor` 는 없다.** 이름은 `staleness`.

**정정 2 — "낡은 것 없음"이 틀렸다.** 인수인계는 *"워커 데몬 3개 전부 오늘 기동
(낡은 것 없음)"* 이라고 단언했다. 실측:

```
HEAD e3b29b4b9c3b committed at 2026-08-25T09:53:40+00:00
STALE  com.bsvibe.worker (pid 18599)              — started 3:31:13 before HEAD
STALE  com.bsvibe.worker-admin (pid 18603)        — started 3:31:12 before HEAD
STALE  com.bsvibe.worker-mac-mini-e2e (pid 18607) — started 3:31:12 before HEAD
```

세 데몬 전부 HEAD 커밋보다 **3시간 31분 먼저** 떠 있었다 — 즉 #822·#823·#824 이전
코드로 돌고 있었다.

**왜 수동 점검이 못 잡았나.** 인수인계가 남긴 수동 루틴은
`ps -eo pid,lstart,command` 로 *"시작 시각 = 낡음의 유일한 신호"* 였다. 그런데 시작
시각만으로는 **기준점이 없다.** "어제 15:22 기동"은 그 자체로 낡았는지 아닌지를
말하지 않는다. 도구가 추가한 것은 정확히 그 기준점 — **HEAD 커밋 시각과의 대조**다.

⇒ 도구는 수동 점검과 "같은 답"을 내지 않는다. **엄격하게 더 낫다.**

### 15.1 처리 — 기준선 → 재시작 → 대조 (규율 9)

되돌리기 전에 쟀다: 활성 런 0(`open/running` 0, `review_ready` 는 종결형) · 대기
승인 0 · 미해결 Decision 0 · `executor_workers` 8행 · `note_embeddings` 1724 ·
활동행 4548.

`launchctl kickstart -k gui/$(id -u)/<label>` × 3 (워커 `kill` 아님 — launchd 관리
재시작). 대조 후:

| | before | after |
|---|---|---|
| staleness | STALE × 3 | **CURRENT × 3** |
| `executor_workers` | 8행 | 8행 (**같은 id**, 하트비트 재개) |
| `note_embeddings` | 1724 | 1724 |
| 활동행 | 4548 | 4548 |

worker identity 는 토큰 기반이라 재등록되면 행이 늘어난다 — 안 늘었다. 지식 오염도 0.

### 15.2 남은 갭

도구는 *"3 stale daemon(s) running pre-HEAD code — restart them."* 이라고 말하지만
**어떻게 재시작하는지는 말하지 않는다.** 정답(`launchctl kickstart -k`)은 이 문서와
메모리에만 있다. 판단 대기 — 도구가 라벨별 명령을 함께 출력할지.

---

## §16 `lint-imports` 16% — §Ⅳ.2 의 열린 질문에 구조적 답

§11.3 은 *"CI 비가시성으로 설명되지 않는다. 아직 모른다"* 로 끝났다. prod 를 다시
세어 **구조를 찾았다.**

### 16.1 검출기 양성 대조군 먼저

§10.1 을 그대로 재현했다 — BSVibe 게이트 142 · 명령있음 94 · `ruff check` 90 ·
`format --check` 42 · `mypy` 72 · `lint-imports` 15. **정확히 일치.** 쿼리를 믿을 수
있다. (제품 3개는 각각 단일 워크스페이스라 §10.1 의 제품별 분할은 이미
`group by workspace_id` 와 동치였다 — §Ⅱ.3 함정 해당 없음.)

### 16.2 `lint-imports` 는 **마지막 슬롯 전용**이다

게이트 안에서 `lint-imports` 가 몇 번째 명령인지 전수:

| 위치 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| 건수 | 0 | 0 | **0** | 9 | 6 |

**4번째 자리 이전에 나온 적이 한 번도 없다.** 그래서 게이트 길이가 곧 가부다:

| 게이트 명령 수 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| 게이트 수 | 3 | 3 | 37 | 37 | 8 | 3 | 2 | 1 |
| `lint-imports` | 0 | 0 | **0** | 3 | 6 | 3 | 2 | 1 |

명령 ≤3 인 게이트 **43건 전부 0건**. ≥5 인 게이트 14건 중 **12건**.
역사적 16% 는 "가끔 빠뜨린다"가 아니라 **94건 중 43건이 애초에 자리가 없었다**이다.

### 16.3 변경 폭은 설명하지 못한다 (경쟁 가설 기각)

"아키텍처성 작업일 때만 관련 있어서 낮다"를 끊었다. `ruff check` 의 파일 목록을
변경 폭 프록시로:

| 변경 폭 | 게이트 | 평균 명령 | `lint-imports` |
|---|---|---|---|
| 1파일 | 6 | 4.00 | 1 (17%) |
| 2–3파일 | 43 | 3.49 | 3 (7%) |
| 4–6파일 | 13 | 4.69 | 6 (**46%**) |
| 7+파일 | 28 | 3.68 | 5 (18%) |

단조가 아니다 — 7+파일(18%)이 4–6파일(46%)보다 낮다. 평균 명령 수는 3.49~4.69 로
평평한데 포함률은 7%~46% 로 갈린다. **폭은 지배 변수가 아니다.**

### 16.4 게이트 길이는 프롬프트 섹션이 늘 때마다 늘었다

deriver 프롬프트에 섹션을 더한 커밋 시각으로 갈랐다:

| 시기 | 게이트 | 평균 명령 | ≥5명령 | `lint-imports` | `format --check` |
|---|---|---|---|---|---|
| pre-#733 (표면 체크 이전) | 14 | 2.64 | 1 | **0 (0%)** | 3 |
| post-#733, pre-#781 | 71 | 3.72 | 7 | 8 (11%) | 36 |
| post-#781 (제약 번역) | 9 | **5.22** | 6 | **7 (78%)** | 3 |

⚠️ **상관이다.** 시기·작업 성격과 교락돼 있고 마지막 칸은 n=9. 원인으로 승격하려면
변수를 하나씩 끊어야 한다 (§Ⅱ.1 이 바로 이 실수였다).

### 16.5 🔴 #822 이후 자연 발생 게이트는 **0건**

내가 처음 `created_at < '2026-08-25 02:56:11+00'`(§11.4 의 기준선)으로 갈라
"post-#822 1건"을 셌다. 그런데 da201d1 의 실제 커밋 시각은 **2026-08-25T07:21:01Z**
다. 그 1건은 **#822 이전**이다.

⇒ #822 의 증거는 여전히 **A/B 뿐**이다. §11.4 가 *"자연 발생 런으로도 세려면 새 런이
쌓인 뒤"* 라고 남긴 것은 아직 쌓이지 않았다. 그리고 **§11.4 가 적어둔 기준선 시각
자체가 커밋 시각이 아니다** — 그걸 그대로 인용하면 pre/post 를 잘못 가른다.

### 16.6 A/B — 프롬프트 버전만 변수 (n=1 스모크)

과거 입력 3건 + 동일 매니페스트 + 동일 모델(`sonnet`, executor)로 프롬프트 빌더만
갈아끼웠다. 옛 버전은 `git show 568e329~1` · `ca211d8~1` 에서 그대로 꺼내
`sys.modules` 에 등록해 exec.

n=1 스모크 12셀 결과 — **`A_pre733`(가장 옛 프롬프트)도 `lint-imports` 를 냈다.**
⇒ 16.4 의 "프롬프트 버전이 원인"은 **그대로 성립하지 않는다.** 옛 프롬프트도 낼 수
있다. n=1 은 판단 근거가 못 되므로 n=5 본실험으로 넘어갔다.

**측정 함정 하나 — 모든 arm 이 동시에 `ExecutorAdapterUnavailable`.** resolver 에
redis 를 안 넘겨서 12셀 전부 0이 나왔다. 균일 실패라 "옛 프롬프트가 못 낸다"로
오독할 뻔했다. *생산자가 꺼져 있으면 0 은 공짜로 나온다.*

### 16.7 모델은 과거에 대해 확인 불가

`routing_decision` 활동 기록은 **2026-08-18 부터만** 있다(총 26행). 그 이전 런이
어떤 모델을 썼는지 이 테이블로는 알 수 없다 — 메모리의
*액티비티 로그로 과거를 세지 마라* 와 같은 함정. 기록된 구간은 전부
`sonnet / workspace_default` 로 A/B 와 동일하다.

### 16.8 본실험 n=5 — 프롬프트 가설 **기각**, 그리고 #822 의 진짜 수치

arm B(pre-#781 프롬프트) vs D(배포본 + CI 블록), 입력 2건(코드 변경), 각 n=6
(스모크 n=1 포함). 총 32셀 · 오류 0.

| arm | 셀 | 평균 명령 | `ruff check` | `format --check` | `mypy` | `lint-imports` | **CI 4개 전부** |
|---|---|---|---|---|---|---|---|
| **B** pre-#781 | 13 | 3.54 | 13 | **0** | 13 | 7 (54%) | **0** |
| **D** 배포본+CI | 13 | 5.23 | 12 | **12** | 12 | 9 (69%) | **9 (69%)** |

(`ruff check`/`mypy`/`lint`/`all4` 의 D 쪽 1건 미달은 전부 입력 `fae09a47` —
*"조사만 하고 파일 쓰지 마라"* 인 조사 런이라 게이트가 2명령으로 나온다. 코드 변경
입력 2건만 보면 B `format` **0/12**, D `format` **12/12** · **all4 9/12**.)

**두 가지가 나왔다.**

**① `format --check` 0→12 는 이전 세션 결과의 독립 재현이다.** 이전 세션은 배포본
프롬프트에서 CI 블록만 on/off 했다(0/8→8/8). 여기서는 **프롬프트 버전 전체**를
갈아끼웠는데 같은 자리가 갈렸다. 다른 대조군, 같은 결론.

**② 새 수치 — CI 4개를 *동시에* 담는 비율 0% → 69%.** `format` 하나가 아니라
"레포가 자기에게 요구하는 검사 전체"를 게이트가 담느냐가 #822 가 닫으려던 간극이다.
그 숫자는 이제까지 잰 적이 없다.

**③ 🔴 프롬프트 가설 기각.** §16.4 는 "게이트 길이가 프롬프트 섹션 수를 따라 늘었고
그래서 `lint-imports` 가 올라갔다"였다. 그런데 **pre-#781 프롬프트가 이 입력들에서
54%** 를 낸다 — 그 프롬프트가 실제로 돌던 시기의 역사값은 **11%**(8/71)다.
같은 프롬프트, 5배 차이. **프롬프트 버전은 원인이 아니다.**

### 16.9 A/B 는 애초에 이 질문의 도구가 아니었다

같은 arm 안에서 입력별로 갈렸다:

| B(pre-#781) | `lint-imports` |
|---|---|
| `e3c08708` (9파일) | 5/6 (**83%**) |
| `010ef006` (1파일) | 2/6 (**33%**) |

**입력 분산이 arm 차이보다 크다.** 역사적 11~16% 는 71~94건의 **이질적인 작업
모집단**에 대한 비율이고, 내 A/B 는 그 중 **2건**을 잰다. 2건으로 모집단 비율을
복원할 수 없다 — 도구가 질문에 안 맞았다.

기각되지 않은 것은 **전수 센서스**(§16.2, n=94)다: `lint-imports` 는 4·5번째 자리
에만 나오고, 명령 ≤3 게이트 43건에서 0건이다. 역사적 비율은 **게이트 길이 분포의
함수**이고, 그 분포를 재는 도구는 A/B 가 아니라 센서스다.

⇒ 질문은 한 층 내려간다: **왜 94건 중 43건이 명령 3개 이하였나.** 다만 D arm 평균이
5.23 이라 #822 이후로는 이 조건이 이미 달라졌을 수 있다 — 자연 발생 게이트가 쌓이면
§16.2 쿼리를 다시 돌려서 센서스로 답할 문제다.

### 16.10 배제한 것들 (다음 세션이 다시 파지 않도록)

| 가설 | 실측 | 판정 |
|---|---|---|
| CI 비가시성 | 이전 세션 A/B: CI 없이도 6/8 | **기각**(§11.3) |
| 프롬프트 버전 | pre-#781 이 54%, 역사값 11% | **기각**(§16.8) |
| 변경 파일 폭 | 7+파일 18% < 4–6파일 46%, 비단조 | **기각**(§16.3) |
| importlinter 설정 성장 | 7/10~8/21 내내 계약 5개, 14~18KB | **기각** |
| 매니페스트 절단으로 안 보임 | `[tool.importlinter]` = 바이트 **3566**, 상한 8192 | **기각** |
| 모델 변경 | `routing_decision` 기록이 8/18 부터만 존재 | **확인 불가**(§16.7) |
| 게이트 길이(슬롯 부족) | ≤3명령 43건 중 0건 · ≥5명령 14건 중 12건 | **남음** |

### 16.11 프로브 위생

* 32셀 전부 `executor_tasks` 로 나갔고 전부 `done`. 오류 0.
* 오염 대조: `note_embeddings` 1724 · 활동행 4548 · `verification_results` 408 ·
  `execution_runs` 228 — **전부 무변동**. 챗 턴이라 런/지식을 안 만든다.
* prod 컨테이너 `/tmp` 의 프로브 파일 6개 삭제 확인.

---

## §17 §Ⅳ.5 항목 C 측정 — 툴 설정을 보여주는 것은 **역효과**다 (2026-08-26)

인수인계 §Ⅳ.5: *"`_MANIFEST_FILES` 에 standalone 툴 설정(`ruff.toml`·`mypy.ini`)은
안 넣었다. §10.4 에서 의도적으로 범위 밖. **CI 만으로 충분한지 측정 후 결정.**"*

### 17.1 전제가 인수인계보다 강했다

코드로 확인하니 상황이 문서보다 심각했다:

* `ruff.toml`(7,587B) · `mypy.ini`(1,593B) 는 **커밋 #1 부터** 레포에 있다.
* `pyproject.toml` 에는 **`[tool.ruff]` 도 `[tool.mypy]` 도 없다.** 설정이 통째로
  그 두 파일에 산다.
* ⇒ deriver 가 pyproject 에서 보는 것은 **의존성 이름뿐**이다
  (`ruff>=0.6` · `mypy>=1.11` · `import-linter>=2.0`). `ruff format` 은 `[format]`
  섹션이 `ruff.toml` 안에 있어서 **선언 자체가 안 보인다.**

이건 §10.1 의 45% 를 *"CI 에만 이름이 있어서"* 보다 잘 설명한다. 그래서
"툴 설정을 보여주면 CI 없이도 format 이 살아난다"를 가설로 세웠다.

### 17.2 A/B — 가설 기각, 그것도 반대 방향으로

같은 과거 입력 2건 · 같은 모델 · CI 블록 없음. 변수는 **매니페스트에 툴 설정을
더하느냐** 하나. 각 n=5, 총 20셀, 오류 0.

| arm | n | `ruff check` | `format --check` | `mypy` | `lint-imports` | **CI 4개 전부** |
|---|---|---|---|---|---|---|
| 배포본, CI 없이 | 10 | 10 | **0** | 10 | **10** | 0 |
| **+ 툴 설정**, CI 없이 | 10 | 10 | **3** | 10 | **3** | 2 |
| *(참고 §16.8)* 배포본 **+ CI** | 13 | 12 | **12** | 12 | 9 | **9** |

* `format --check` 0 → 3/10. **살아나긴 하나 CI 블록(12/13)의 발끝에도 못 간다.**
* `lint-imports` **10/10 → 3/10.** 있던 근거가 죽었다.
* 순효과 음수.

### 17.3 왜 — 밀어내기

`ruff.toml` 은 7,587B 이고 그 대부분이 `[lint.per-file-ignores]` 의 경로 나열이다.
그게 프롬프트에 들어가면 pyproject 의 `[tool.importlinter]`(바이트 3566에서 시작)
근거를 밀어낸다. **길이가 근거를 이긴다.**

이건 같은 날 고친 §18 의 바이트 예산 결함과 **같은 현상**이다 — 그쪽은 CI 블록이
매니페스트를 밀어내는 것이고, 이쪽은 매니페스트가 자기들끼리 밀어낸다.
`_CI_TOTAL_CTX_BYTES` 주석이 말하는 *"a repo with 40 workflows would otherwise
crowd the manifests out"* 이 실측으로 확인된 셈이다.

### 17.4 판정

⇒ **항목 C 는 닫는다.** `_MANIFEST_FILES` 에 standalone 툴 설정을 넣지 않는다.
CI 블록이 그 자리를 채우는 올바른 채널이고 #822 의 선택이 맞았다.
§10.4 의 의도적 범위 밖 결정은 **측정으로 정당화됐다.**

⚠️ 다만 이건 **CI 를 선언하는 레포**에 대한 결론이다. CI 가 없고 툴 설정만 있는
레포는 여전히 `ruff format` 선언이 안 보인다 — 그 경우는 아직 안 쟀다.

---

## §18 PR #825 — CI 근거 수집의 바이트 예산 (2026-08-26)

인수인계 §Ⅳ.5 가 *"실무상 무해하나 정확하지 않다"* 로 남긴 것. 실측하니 무해하지 않다.

### 18.1 결함

`_read_ci_declarations` 는 예산을 **바이트**로 선언해놓고 읽기만 바이트로 하고
**보관과 차감은 문자**로 했다:

```python
text = data.decode("utf-8", errors="replace").strip()[:remaining]  # 문자를 자른다
remaining -= len(text)                                             # 문자를 뺀다
```

| | 청구 | 실제 소비 | 예산 |
|---|---|---|---|
| 한국어 CI 5파일 | 13,655 | **40,965 B** | 24,576 B (**1.67배**) |
| 파일 1개 | — | **8,193 B** | 8,192 B |

파일당 1바이트 초과는 `errors="replace"` 가 넣는 U+FFFD(3B)에서 온다.

### 18.2 왜 무해하지 않은가

상한이 존재하는 이유가 코드에 적혀 있다 — *"a repo with 40 workflows would
otherwise crowd the manifests out of the prompt entirely."* 초과는 곧 **매니페스트
근거가 밀려나는 것**이고, 그 근거는 `_manifest_present()` 의 fail-closed 판정을
겸한다. §17.3 이 그 밀어내기를 독립적으로 실측했다.

BSVibe 자기 CI 파일은 비ASCII 가 9바이트뿐이라 이 레포엔 영향이 없다. **이 코드는
임의의 레포를 위한 인프라다.**

### 18.3 기존 테스트가 알리바이였다

`test_the_total_byte_budget_is_bounded` 는 `"x" * 200_000` 으로 잰다. ASCII 라
문자 수 == 바이트 수여서 **문자로 센 예산도 영원히 통과한다.**

### 18.4 방향 · 검증

`_clamp_utf8()` 하나 추가 — 인코딩된 형태를 잘라 문자 경계에서 끊는다. 읽기 상한 ·
보관 · 차감을 전부 바이트로 통일. ⚠️ `_read_repo_manifests` 는 안 건드림(#822 불변).

* RED 2건(올바른 이유) → GREEN 15건
* **음성 대조군 2층**: 헬퍼 무력화 → per-file 만 실패 / 문자 차감 복원 → total 만 실패
* fresh PG 전체 **6394 passed · skip 1**(Redis) · 커버리지 **91.13%**
* `ruff check` · `format --check` · `mypy` · `mypy --strict` · `lint-imports` 전부 통과
  (⚠️ 최초 `format --check` 가 내 두 파일을 잡았다 — 이 트랙이 다루던 바로 그 검사다)

---

## §19 PR #826 — 진단이 자기 처방을 말한다 (2026-08-26)

### 19.1 원인 — 실사용에서 나왔다

§15 에서 `bsvibe-worker staleness` 를 **처음 실제로** 썼다. 진단은 정확했고
거기서 멈췄다 — `"3 stale daemon(s) running pre-HEAD code — restart them."`

읽는 쪽은 세션 첫머리의 **에이전트**다. 명령을 도구 밖에서 구해와야 하는데
**가장 싼 추측인 `kill` 이 오답**이다: launchd 가 수명주기를 소유하고 워커의
identity 는 그 서비스 정의가 나르는 토큰이다.

⇒ 결함을 짚어놓고 고치는 법을 말하지 않는 도구는 읽는 이에게 **올바른 명령과
파괴적인 명령 사이의 동전 던지기**를 넘긴다. 형님 원칙 그대로 — 사람은 안 보이면
딴 데를 뒤지지만 에이전트는 **주어진 것만 본다.**

### 19.2 방향 — 새 기계 0개, 출력 한 블록

stale 데몬마다 `launchctl kickstart -k gui/<uid>/<label>` 한 줄 + `kill` 이 아닌
이유를 **읽는 이가 이미 보고 있는 자리**에. `uid` 는 주입해서 렌더러를 순수하게
유지한다(이 모듈이 프로세스·git 조회를 주입하는 것과 같은 규율).

### 19.3 실증 — 실제 왕복

#825 가 머지되자 데몬 3개가 **다시 STALE** 이 됐다. 이 PR 이 찍어준 명령 세 줄을
그대로 실행 → 전부 CURRENT. 대조: `executor_workers` **8행 동일**(identity 보존) ·
`note_embeddings` 1724 · 활동행 4548 **무변동**.

* RED 4 → GREEN 12 · 음성 대조군 2층(처방 줄 제거 / CURRENT 까지 처방) 각각 자기
  테스트만 실패 · 양성 대조군(CURRENT 만 = 처방 없음)은 변경 전에도 green
* fresh PG **6399 passed · skip 1** · 커버리지 91.13% · 전 게이트 통과

---

## §20 이 세션 상태 (2026-08-26)

| | |
|---|---|
| prod | **`7fb873f`** (#826 배포 확인) |
| 머지 | **#825 · #826** (둘 다 배포·실증 완료) |
| 열린 PR | **0** |
| 대기 승인 · 미해결 Decision | **0 · 0** |
| 워크트리 · 테스트 컨테이너 | **0 · 0** · main 트리 추적 변경 **0** |
| 워커 데몬 | 3개 전부 CURRENT (오늘 세 번 재시작 — 머지마다) |

### 20.0 #826 prod 실증

배포된 `7fb873f` 의 `staleness` 가 스스로 처방을 찍었고, **그 세 줄을 그대로 실행**해
전부 CURRENT 로 돌아왔다. 대조: `executor_workers` **8행 동일**(identity 보존) ·
`note_embeddings` 1724 · 활동행 4548 **무변동** · 활성 런 0.

⇒ 도구가 낸 명령이 실제로 진단을 해소한다 — 그럴듯한 오답이 아니다.

## §21 §16.9 종결 — 짧은 게이트의 원인은 프롬프트 버전이다 (2026-08-26)

§16.9 는 *"왜 94건 중 43건이 명령 3개 이하였나 — 센서스로 답할 문제고 #822 이후
게이트가 쌓여야 한다"* 로 남겼다. **새 런을 기다릴 필요가 없었다.** 이미 가진 두
데이터로 답이 나온다.

### 21.1 역사 — 짧은 게이트 비율이 시기마다 단조로 줄었다

| 시기 | 게이트 | **≤3 명령** | 평균 |
|---|---|---|---|
| pre-#733 | 14 | 12 (**86%**) | 2.64 |
| post-#733, pre-#781 | 71 | 29 (**41%**) | 3.72 |
| post-#781 | 9 | 2 (22%) | 5.22 |

### 21.2 A/B 가 그것을 통제 실험으로 재현한다

§16.8 의 A/B 를 **길이 축으로 다시 읽었다.** 같은 과거 입력 2건 · 같은 모델 ·
프롬프트 버전만 변수 (코드 변경 입력만, 셀 단위):

| arm | n | 평균 | **≤3 명령** | ≥5 명령 |
|---|---|---|---|---|
| B pre-#781 | 12 | 3.58 | **5 (42%)** | **0** |
| C 배포본(CI 없이) | 12 | 4.50 | **0** | 6 |
| D 배포본 + CI | 12 | 5.50 | **0** | 10 |

**역사적 post-#733 시기 41% ↔ 그 시기 프롬프트를 그대로 돌린 42%.** 재현됐다.
그리고 B arm 은 ≥5 명령 게이트를 **한 번도** 못 낸다 — `lint-imports` 가 4·5번째
자리에만 나오므로(§16.2) 굶는 것이 당연하다.

⇒ 사슬이 닫힌다: **프롬프트 버전 → 게이트 길이 → 슬롯 가용성 → `lint-imports`.**
§16.8 에서 내가 기각한 것은 *"프롬프트 버전이 lint-imports 비율을 설명한다"* 였고
그건 여전히 기각이다(입력 분산이 지배). **길이**는 설명한다 — 그리고 길이가
슬롯을 만든다.

### 21.3 조건은 이미 사라졌다

배포본은 ≤3 명령 게이트를 **24셀 중 0건** 낸다. `lint-imports` 를 굶기던 조건
자체가 없다. 자연 발생 게이트가 쌓이면 §16.2 쿼리로 확인만 하면 된다 —
**답을 기다리는 게 아니라 예측을 확인하는 것**이다.

---

## §22 §Ⅳ.1 — 배포된 프론트엔드 사슬 실증 (2026-08-26)

§Ⅳ.1 은 *"`merge_conflict_review` Decision 이 생기고 형님이 버튼을 눌러야 닫힌다"* 
였다. **전제 하나가 틀렸다.**

### 22.1 `retry` 는 병합 충돌 전용이 아니다

`_CHECKPOINT_ACTIONS_BY_KIND` 실측 — 네 kind 가 `retry` 를 제공한다:

| kind | 라벨(ko) |
|---|---|
| `run_drive_failed` | 다시 시도 |
| `verification_failed` | 다시 시도 |
| `human_review_required` | 다시 시도 |
| `merge_conflict_review` | **지침 주고 다시 시도** |

#824 의 배선은 `resolve_checkpoint` 에 있어 **kind 무관**이다. 즉 드문 병합 충돌을
기다릴 이유가 없었다.

### 22.2 배포된 번들에서 사슬 전체를 읽었다

`https://app.bsvibe.dev` 의 `/_next/static/chunks/12x4~tewn3-ws.js` (배포본):

```js
[p,h]=useState(""), b=p.trim()                     // 항상 보이는 입력 — 숨은 토글 없음
<input placeholder={r("guidancePlaceholder")} value={p} onChange={e=>h(e.target.value)}>
async function I(t){ await resolveCheckpointAction(e.checkpointId, t, b) }   // 클릭이 b 를 넘긴다
function(e,a,r){ let n=(r??"").trim(), i={action_key:a}; return n&&(i.reason=n), apiFetch(...) }
```

타이핑한 텍스트 → `b` → `reason` → POST 바디. **#824 이전에 버려지던 그 값이
배포본에서 실제로 실린다.**

### 22.3 정직하게 남은 것

이것은 **배포된 산출물의 코드 검증**이지 브라우저 클릭이 아니다. 백엔드 절반은
이미 7/7 + 실 ASGI 클라이언트로 증명됐다. 남은 것은 **사람 손가락 하나** —
렌더링/이벤트 층뿐이고 코드 경로는 확인됐다.

형님이 다음에 그 버튼을 쓰실 때 확인:
```sql
select payload->'resolved_decisions' from execution_runs
where payload::jsonb ? 'resolved_decisions' order by created_at desc limit 3;
```

---

### 20.1 다음 세션이 이어받을 것

1. **§Ⅳ.1 PWA→prod retry 왕복** — 여전히 미실증. 미해결 Decision 0건이라
   **누를 버튼 자체가 없다.** 자연 발생 대기.
2. **§16.9 의 남은 질문** — *왜 게이트 43/94 가 명령 3개 이하였나.* A/B 가 아니라
   **센서스**로 답할 문제고, #822 이후 자연 발생 게이트가 아직 **0건**이라
   쌓여야 한다. 쌓이면 §16.2 쿼리를 그대로 다시 돌려라.
3. **§Ⅳ.4 #821** — 재현 대기, 하네스에 관측 심어둠.
4. **§17.4 의 남은 구멍** — CI 를 선언하지 않고 툴 설정만 있는 레포는 아직 안 쟀다.

### 20.2 세션 첫 일 (갱신)

```bash
cd /Users/blasin/Works/bsvibe-app/main
.venv/bin/bsvibe-worker staleness      # ⚠ `doctor` 아님. #826 머지 후엔 명령까지 찍어준다
curl -s https://api.bsvibe.dev/api/health
```

---

## §23 #821 — 재현 106회 실패, 그러나 가설 하나가 크게 약해졌다 (2026-08-26)

인수인계 §Ⅳ.4: *"가짜 워커가 왜 60초 동안 태스크를 못 받았는지 여전히 모른다.
재현 불가. CI 에서 또 나면 하네스가 스스로 지목한다."*

### 23.1 재발 여부는 아직 근거가 못 된다

#821 머지(2026-08-25T05:46Z) 이후 `ci.yml` **12런 전부 success**. 그런데 최근
100런의 실패는 **4건**이고 이 flake 는 그중 **1건**이다 — 기저율 ~1/100.
12런으로 "안 났다"는 검정력이 거의 없다. **재발 없음 ≠ 고쳐짐.**

### 23.2 재현 시도 — 네 조건, 106회, 0건

| 조건 | 시도 | 재현 |
|---|---|---|
| 무부하 (macOS, 12코어) | 40 | 0 |
| 10중 동시 실행 경합 (macOS) | 30 | 0 |
| `--cpus=2` Linux 컨테이너 | 30 | 0 |
| `--cpus=0.25` (극단) | 6 | 0 |

### 23.3 🔑 중요한 것은 실패 수가 아니라 **소요 시간**이다

극단 스로틀(호스트 CPU 의 **1/32**)에서도 파일 10개 테스트가 **6.8초**에 끝난다.
평소 1.1초 대비 **6배**. 그런데 `_FAKE_WORKER_BUDGET_S = 60` 을 소진하려면
**~55배** 느려져야 한다.

⇒ **CPU 기아만으로는 이 증상이 안 난다.** 2코어 러너라는 설명은 6배 수준의 차이를
주는데 필요한 것은 55배다. 하네스가 구분하도록 만들어둔 세 형태 중
**"read 가 적다(기아)"는 크게 약해졌고**, 남는 것은:

* **read 는 많은데 본 게 없다** → 스트림/키가 어긋났다
* **action 이 seen 에 찍힌다** → 다른 action 이 왔다
* (하네스 밖) **태스크가 애초에 XADD 되지 않았다** → 프로덕션 디스패치 쪽

### 23.4 이미 배제된 것 (재조사 금지)

* `$` 레이스 — 하네스의 `last_id` 는 `"0"` 으로 시작한다. 먼저 도착한 태스크도 본다
* SQLite 락 · fakeredis block · 테스트 병렬 오염 — #821 이 측정으로 기각
* **CPU 기아** — 이번 §23.3 (필요 55배 vs 관측 6배)

### 23.5 다음에 나면

하네스가 `reads` · `seen` · `elapsed` 를 던진다. **그 세 숫자를 인수인계에 그대로
옮겨라** — 그것이 위 세 형태 중 하나를 지목한다. 그때까지 **프로덕션 코드는 건드리지
않는다**(#821 의 원칙 그대로).

---

## §24 남은 항목 정리 — 형님 지시 *"B는 닫아. A 처리 진행해"* (2026-08-26)

### 24.1 ✅ 항목 C 닫힘 — `_MANIFEST_FILES` 에 standalone 툴 설정을 넣지 않는다

형님 확정. §17 의 측정이 근거다(`lint-imports` 10/10 → 3/10). `ruff.toml` ·
`mypy.ini` 는 레포에 있지만 **deriver 에게 보여주지 않는다.** CI 블록이 그 자리를
채우는 올바른 채널이고 #822 의 선택이 맞았다.

⚠️ 이 결정은 `_MANIFEST_FILES` 를 **피처 플래그로 겸하는 곳**을 건드리지 않는다
(`_manifest_present()` fail-closed · `inplace_gate`). 그래서 무해하다.

### 24.2 ✅ §17.4 의 "남은 구멍"은 구멍이 아니었다 — 이미 쟀다

§17.4 에 *"CI 를 선언하지 않는 레포는 아직 안 쟀다"* 고 적었는데, **arm C 와 E 는
둘 다 `ci_declarations=None`** 이다. 즉 그 조건이 이미 실험 안에 있었다:

| arm (둘 다 CI 없음) | n | `ruff check` | `format --check` | `mypy` | `lint-imports` |
|---|---|---|---|---|---|
| pyproject 만 | 12 | 12 | **0** | 12 | **11** |
| + 툴 설정 | 10 | 10 | 3 | 10 | **3** |

CI 없는 레포에서도 툴 설정 추가는 `format` +3 · `lint-imports` **−8**. **순효과 음수.**
⇒ §17.4 의 유보를 철회한다. 결론은 CI 유무와 무관하다.

**교훈**: 실험을 쓰고 나서 *"이 조건은 안 쟀다"* 고 적기 전에 **arm 의 인자를 다시
읽어라.** 내가 안 본 것은 조건이 아니라 내가 그 arm 을 그 이름으로 안 부른 것뿐이었다.
(→ [[reread-the-experiment-you-already-ran-on-a-different-axis]] 와 같은 실수, 같은 날 두 번)

### 24.3 §21.3 — 자연 트리거 날짜가 있다

`workspace_schedules` 실측: BStockReport 주간 스케줄이 **enabled**,
`cron_expr = '30 0 * * 1'`, `next_run_at = 2026-08-31 00:30:00+00`
(= 월 09:30 KST). ⚠️ cron 은 **UTC 평가**다.

⇒ **2026-08-31 에 게이트가 생긴다.** 그날 이후 §16.2 쿼리를 돌려 배포본이 ≤3 명령
게이트를 안 내는지 확인하면 §21.3 이 닫힌다. (⚠️ 그 런은 BStockReport 제품이므로
§16.1 의 BSVibe 전용 숫자와는 별도 모집단이다 — 예측은 길이에 관한 것이라 유효하다.)

### 24.4 §Ⅳ.1 · #821 — 구조적으로 외부 사건이 필요하다

* **§Ⅳ.1**: 배포 번들 사슬은 §22 에서 확인됐다. 남은 것은 **형님이 `retry` 를
  지침과 함께 한 번 누르는 것**. `retry` 는 네 kind 가 제공하므로 병합 충돌을
  기다릴 필요 없다. **일부러 만들지 않는다** — 해소 시 지식 노트가 생기고,
  UI 이벤트 핸들러 하나에 치를 값이 아니다.
* **#821**: §23 이 CPU 기아를 기각했다. 다음 발화 때 하네스가 `reads`·`seen`·
  `elapsed` 로 지목한다. 기저율 ~1/100 이라 기다리는 것 외에 할 일이 없다.

### 24.5 🔴 곁가지 — 마스터 상태 문서가 틀렸다 (미처리)

`docs/STATUS.md`(Aug 18) 검증:

| 문서가 말하는 것 | 실측 |
|---|---|
| R2 cutover *"아직 `local`"* | ❌ prod `BSVIBE_PRODUCT_BUNDLE_BACKEND=s3` — **컷오버 완료** |
| launchd worker **2대** | ❌ **3대** (`worker` · `worker-admin` · `worker-mac-mini-e2e`) |

`BSVibe_Roadmap.md` 는 **2026-06-24** 로 2개월 지났다. 최근 두 달의 실제 트랙
(신뢰 래칫 · 판정 축 · 구조 삭제 · 검증 게이트)은 세션 인수인계에만 있다.
⇒ **백로그 문서를 근거로 다음 트랙을 정하면 틀린 전제 위에서 결정하게 된다.**

확인 안 한 것: `_REMOTE_TOOL_EXECUTORS = {"claude_code"}` 만이라 **executor 파리티는
3종 중 1종** 그대로다(prod 에 codex 계정 1개가 있는데 agentic 작업 불가).
Product tick · Reality Audit 미해결 항목 · Production Verification 트랙은 미확인.

⚠️ **자격증명 노출**: prod 컨테이너 env 를 덤프하며 `BSVIBE_PRODUCT_BUNDLE_S3_SECRET_KEY`
와 access key 가 세션 로그에 찍혔다. R2 키 로테이션은 형님 판단.

---

## §25 🔴 #821 재발 — 하네스가 **틀린 가설을 계측**하고 있었다 (2026-08-26)

§23 은 *"다음에 나면 하네스가 `reads`·`seen`·`elapsed` 로 지목한다"* 로 끝났다.
그날 안에 재발했고, **지목하지 못했다.** 왜 못했는지가 이번 소득이다.

### 25.1 사실

PR #828 의 CI(`32931764301`)에서
`tests/supervisor/sandbox/test_client_worker_manager.py::test_exec_failing_command_maps_nonzero_exit`
실패. **제 diff 와 무관**(요약·매니페스트 쪽 변경).

| | |
|---|---|
| 소요 | **70.8초** (05:03:26.35 → 05:04:37.15) = `timeout_s(10) + _AWAIT_SLACK_S(60)` |
| 실패 단언 | `assert result.timed_out is False` — **업무 단언**, 하네스 고발이 아님 |
| 하네스 고발 | **발화 안 함** |

### 25.2 고발이 안 났다는 것이 곧 진단이다

`_run_one_exec_task` 는 exec 을 보면 실행 → `record_result` → commit → `return` 한다.
못 보면 예산 소진 후 `AssertionError` 를 던진다. **정상 종료했다** ⇒ 워커는
태스크를 **보고, 실행하고, 결과를 기록했다.**

그리고:

* `_TERMINAL_STATUSES = ("done", "failed")` — 실패한 exec 도 **terminal** 이다
* `await_completion` 은 `_AWAIT_POLL_INTERVAL_S = 2.0` 으로 **2초마다 DB 재폴링**한다

⇒ pub/sub 신호 유실로는 70초를 설명할 수 없다. 폴링이 2초 안에 풀었어야 한다.

### 25.3 ⇒ 실패 모드가 #821 의 전제와 다르다

| | #821 이 계측한 것 | 실제로 일어난 것 |
|---|---|---|
| 가설 | 워커가 **태스크를 못 받았다** | 워커는 받았고 **결과까지 기록**했다 |
| 계측 위치 | 워커 루프(`reads`·`seen`·`elapsed`) | — |
| 발화 가능? | **원리상 불가** — 워커는 정상 종료한다 | |

**다음 계측은 요청자 쪽에 붙어야 한다**: `await_completion` 이 몇 번 폴했는지 ·
마지막으로 본 `status` 가 무엇이었는지 · 경과. 지금은 `TaskTimeout` 이 그 셋을
하나도 안 들고 나온다.

### 25.4 배제한 것 (이번 라운드)

* **워커/요청자가 다른 DB** — 아니다. 둘 다 같은 `shared_file_sessionmaker`
  (파일 SQLite, WAL, 세션별 커넥션)
* **요청자가 stale snapshot 을 든다** — 아니다. `session_factory` 가 배선돼 있어
  읽기마다 짧은 세션을 연다. **프로덕션도 동일**(`sandbox_selection.py` → 매니저
  생성자 → `_read_terminal_isolated(session_factory=…)`)
* **CPU 기아** — §23 에서 이미 기각(필요 55배 vs 관측 6배)

### 25.5 남은 후보

`record_result` 의 commit 이 실제로 관측 가능해지는 시점과, 요청자의 폴링 루프가
실제로 도는지(`pubsub.get_message(timeout=…)` 가 부하 하에서 요청한 시간보다 오래
잡으면 DB 읽기가 밀린다). **재현 없이 프로덕션 코드를 고치지 않는다**(#821 원칙) —
계측을 옮기는 것이 다음 수순이다.

### 25.6 #828 처리

이 flake 는 **선재하고 무관**하다(#819 도 물었다). #828 은 로컬 fresh PG
6409 passed · 전 게이트 통과이고 CI 에서도 pytest 6405 passed 뒤 이 한 건만
떨어졌다. 위 분석을 마친 뒤 실패 잡만 재실행했다 — **분석 없는 재실행이 아니다.**

---

## §26 세션 종료 상태 (2026-08-26)

| | |
|---|---|
| prod | **`49f5c70`** |
| 머지 | **#825 · #826 · #827 · #828 · #829** — 전부 배포 확인 |
| 열린 PR | **0** |
| 대기 승인 · 미해결 Decision | **0 · 0** |
| 워크트리 · 테스트 컨테이너 | **0 · 0** · main 트리 추적 변경 **0** |

### 26.1 #829 — 매니페스트 파일당 상한

#827 의 docstring 이 *"Bounded per-file AND in total"* 이라 주장했는데 per-file 이
거짓이었다. 바이트로 자른 읽기가 문자 중간에서 끝나면 `errors="replace"` 가 3바이트
U+FFFD 를 넣어 **읽은 바이트보다 크게** 재인코딩된다(실측 8193B / 상한 8192).
`min(_MANIFEST_CTX_BYTES, max(remaining, 0))` 한 줄. 키 존재 판정은 클램프 이전
텍스트로 결정되므로 불변식 그대로.

실무 영향은 1바이트라 사실상 없다. 고친 이유는 크기가 아니라 **주장이 거짓**이었기
때문이고, 주장을 약화시키는 대신 참으로 만들었다.

### 26.2 이 세션의 사슬

```
staleness 첫 사용 → 인수인계가 틀림(낡은 데몬 3개)
  → #826 진단이 자기 처방을 말한다
lint-imports 16% 규명 → 가설 5개 기각, 슬롯 부족이 원인
  → §17 툴 설정 추가는 역효과(항목 C 닫음)
  → #825 CI 근거 바이트 예산(같은 밀어내기의 다른 층)
  → 도그푸딩 런 발주 → #827 매니페스트 총량 예산
     → 그 런의 요약이 숫자를 틀리게 말함 → #828
     → 그 PR 의 CI 에서 #821 재발 → §25 계측이 틀린 곳에 있었음
  → #829 매니페스트 파일당 상한(#827 의 잔여)
```

측정 하나가 다음 측정을 낳았고, 도그푸딩이 자기 결함을 드러냈다.

---

## §27 #830 — 계측을 요청자 쪽으로 옮겼다 (2026-08-26)

§25 가 지목한 다음 수순을 실행했다.

### 27.1 무엇이 없었나

`TaskTimeout` 은 `f"executor task {id} did not complete within {timeout_s}s"`
한 줄이 전부였다. 폴을 몇 번 했는지 · 행이 어떤 상태였는지 · 실제 경과가
얼마인지 **셋 다 없었다.** 그래서 §25 의 진단을 *"고발이 안 났다"* 는 침묵에서
역추론해야 했다.

### 27.2 세 숫자가 형태를 가른다

| 관측 | 뜻 |
|---|---|
| `polls` ≈ 예산/주기 + `last_status="dispatched"` | 워커가 끝내 보고 안 함 |
| 같은 `polls` + `last_status=None` | 행이 읽기에 **안 보임**(가시성 문제) |
| `polls` 가 그 비율보다 **훨씬 낮음** | 루프가 안 돌았다 — 대기가 굶음 |
| `last_status` 가 **terminal** | 행은 완료였는데 읽기가 전부 놓쳤다(가장 심각) |

### 27.3 설계 요점 — 합침이 범인이었다

`_read_terminal_isolated` 는 *"행 없음"* 과 *"아직 실행 중"* 을 **둘 다 `None`**
으로 접는다. 그 합침이 타임아웃을 못 읽게 만들었다. raise 경로 전용
`_read_status_isolated()` 를 따로 뒀고 **한 번만** 부른다 — 폴링 루프의 쿼리
수는 그대로다.

**소비자까지 배선**했다(`SandboxResult.stderr` + 구조화 로그). 진단이 예외에만
있으면 CI 로그엔 여전히 `exec timed out after 70s` 만 찍혀 다음 조사가 또 0에서
시작한다 — 정확히 2026-08-26 에 일어난 일이다.

### 27.4 ⚠️ 동작은 한 줄도 안 바꿨다

#821 원칙 유지. **원인은 여전히 모른다.** 이 PR 은 결함을 고친 것이 아니라
**다음 조사를 가능하게** 만든 것이다. 다음 발화 때 위 표가 지목한다.

검증: RED 4 → GREEN · 음성 대조군 **3층** 각각 자기 층만 실패 ·
6415 passed · skip 1 · 전 게이트 통과 · CI 6412 passed · skip 4(정합).

---

# 파트 5 — 감사 재측정 + 그 결과 정리 (2026-08-26/27 세션 후반)

## §28 서브에이전트 재측정 — 감사의 57% 가 이미 해소돼 있었다

`internal-docs:BSVibe_Reality_Audit_2026-07-14.md` 를 전수 재측정(서브에이전트).
**77건 중 44건(≈57%)에서 ❌ 표기가 더 이상 유효하지 않았다.**

⚠️ **서브에이전트 숫자를 액면으로 받지 않았다.** 가장 무거운 둘을 직접 쟀고
**하나는 달랐다**:

| 항목 | 서브에이전트 | 내가 직접 잰 값 |
|---|---|---|
| 폐기 개념 (`concepts/active` 中 `retracted_at`) | 403 / 932 | **403 / 932** ✅ 일치 |
| LLM 자기선언 통과 | 50건 · 17 PROVED | **32건 · 8 PROVED** ❌ 다름 |
| semantic index 코사인(본문 대비) | 0.5711 | **0.7006** (노트마다 다름, 방향 동일) |

⇒ 재측정 결과도 **재측정해야 한다.** 방향은 맞았고 크기는 틀렸다.

## §29 형님 지시 순서대로 처리 (① → ⑤)

| | PR | 핵심 |
|---|---|---|
| ① 철회 개념 누수 | **#831** | 403/932 가 계약·답변에 되인용. `answer_grounding` 이 규칙을 적어놓고 `kind!="note"` 로 면제 |
| ② LLM 자기선언이 게이트를 이김 | **#832** | `gate_expected` 를 계산만 하고 안 씀. 등급 사다리(#780) 삭제가 문을 다시 열었다 |
| ③ `sandbox_enabled` | **#833 + #834** | 침묵은 결정이 아니다. ⚠️#833 단독으로는 **못 막았다**(§30) |
| ④a `daily_brief` | **#835** | 76건 발행 중인데 UI 가 "producer 없음"이라며 비활성 — 끌 수조차 없었다 |
| ④b retract sweep | **#836** | 코드가 세 곳에서 sweep 을 약속하고 인덱스까지 있는데 sweep 만 없었다 |
| ④c semantic index | **#837 + #838** | 쓰기 지점 수정 + `content_hash` 로 기존 1,724행 자동 백필 |
| ⑤ 파리티 | — | 계약 2/3 실증, 인증 배선이 선행 조건 (§31) |

## §30 🔴 #833 이 실제 prod 경로를 못 막았다 — 층을 하나 덜 따라갔다

#833 은 `BSVIBE_SANDBOX_ENABLED` 가 **제공되지 않았을 때** 기동을 거부하게 했다.
그런데 compose 가 그 상황을 **절대 안 만든다** — `${VAR:-false}` 가 값을 지어내
채운다. 실측(`docker compose config`, 변수 미설정):

```yaml
environment:
  BSVIBE_SANDBOX_ENABLED: "false"     # compose 가 지어낸 값
```

⇒ Settings 는 **항상** "명시적으로 제공됨"으로 보고, 검증기는 **영원히 발화하지
않는다.** 테스트 8개·음성 대조군 3층·전 게이트 통과·배포까지 하고도 그랬다.

#834 가 `:?` 로 고쳤다(compose 자체가 거부). **배포 자체가 검증이었다** — compose 가
이제 그 변수를 요구하는데 배포가 통과했으므로 `.env.prod` 경로가 살아있다.

→ 메모리 `the-layer-below-can-answer-for-the-layer-you-guarded`

## §31 파리티 — 계약 2/3, 3번째는 인증이 선행 조건

| 요구 | 상태 |
|---|---|
| HTTP MCP + 인증 헤더 | ✅ `codex mcp add --url --bearer-token-env-var` |
| 툴 표면 **조회** 검증 | ✅ 격리 `CODEX_HOME` → `mcpServerStatus/list` **0개** 실측 · `experimentalFeature/list` 가 빌트인 7개의 `enabled` 를 읽어준다 |
| 빌트인 끄기가 **실제로** 먹는가 | ❌ **미실증** |

3번째 프로브가 실패한 이유가 설계를 바꾼다: **격리된 `CODEX_HOME` 에는 인증이 없다.**

```
401 Unauthorized  wss://api.openai.com/v1/responses
```

호스트 MCP 서버를 배제한 그 조치가 `~/.codex/auth.json` 도 배제했다. LLM 호출이
일어나지 않았으므로 툴 표면 질문은 열려 있다.

**이 레포에 이미 답이 있다**: `claude_auth.py` 가 정확히 같은 형태다 — launchd
워커가 Keychain 에 못 닿아서 워커가 자기 소유 자격증명 파일을 들고 만료를 관리하며
호출마다 주입한다. codex 도 워커가 `auth.json` 을 per-run 격리 홈에 **의도적으로
심는** 모양이 된다.

→ 메모리 `isolation-erases-the-ambient-condition-under-test`

## §32 이 세션이 반복해서 만난 것 — 알리바이 테스트 6번

전제가 거짓이 된 뒤에도 동작을 계속 고정하는 테스트:

| PR | 알리바이 |
|---|---|
| #825 | `"x" * 200_000` — ASCII 라 바이트/문자 혼동을 원리상 못 잡음 |
| #828 | 요약이 `written_paths` 를 세는 것을 계약으로 고정 |
| #832 | **사라진** 등급 라우팅을 근거로 `PASSED` 를 고정 |
| #835 | 거짓 전제("producer 없음")로 비활성 토글을 고정 |
| #837 | 노트를 안 쓰고 훅만 불러 "summary 를 임베딩한다"를 고정 |
| #838 | `test_alembic_fresh` 가 head 를 리터럴로 **세 군데** |

여섯 번 다 **고치려 할 때 정당하게 실패**하면서 드러났다. 테스트가 깨진 게 아니라
깨져야 할 것이 깨진 것이다.
