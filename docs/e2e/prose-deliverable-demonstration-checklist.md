# E2E — 산문·데이터 산출물도 증거를 벌 수 있다

대상: `_run_outcome_demonstration` 의 artifact 표면 + `artifact_planner_messages`
+ `summarize(contradiction_fails=…)`. 검증 설계 SoT §8.1~§8.3 (트랙 C1).

배경: 게이트 파생기는 산문 산출물을 *"the judge and demonstration paths cover it"* 이라며
`applicable: false` 로 비켰는데, demonstration 경로는 `_is_code_path` 가 아니면 `None` 을
냈다. **아무도 안 맡아서** 비개발 산출물은 정의상 등급 D 였고 매니페스트 있는 레포에선
매번 사람을 불렀다. I2 기계 자체는 이미 표면 중립이었다 — 막던 건 입구 하나.

⚠️ 이 확장의 급소는 §4 규칙 1 이다. 산출물 텍스트를 본 플래너는
`grep "<방금 읽은 문구>"` 를 쓴다 — **모든 산문이 자동 통과**. 그래서 artifact 플래너는
텍스트에 **눈이 멀어** 있고, 그 판정은 **advisory**(벌 수만 있고 떨어뜨릴 수 없음)다.

## 라이브 검증

- [ ] **산문 산출물이 등급 D 를 벗어난다** ⭐
      매니페스트 있는 레포(bsvibe-app)에 리포트/조사 성격 런을 투입해 `.md` 만 나오게 한다.
      `verification_results.result.outcome_demonstration.verdict == "demonstrated"` 이고
      `honesty_grade` 가 **B**. 파킹(`weak_evidence_no_gate`) **없이** 종료.
      (수정 전 재현: `outcome_demonstration` 이 `null`, 등급 D, Decision 파킹)

- [ ] **플래너가 자기가 채점할 텍스트를 못 본다** ⭐⭐
      워커 로그에서 그 런의 플래너 호출 프롬프트를 확인 — 산출물 **경로**는 있고
      **본문 문장은 없다**. blob 의 `surface == "artifact"`.
      *이게 무너지면 이 기능은 등급을 벌어주는 게 아니라 날조한다.*

- [ ] **artifact 프로브는 실제로 돈다**
      `outcome_demonstration.probes[].command` 가 산출물 파일을 지목하고
      `status` 가 `matched`. `output` 에 진짜 관측값이 있다(빈 문자열 아님).

- [ ] **못 맞춰도 실패시키지 않는다** ⭐
      플래너가 표현을 잘못 짚은 런(예: 한글 리포트에 영문 토큰을 기대) →
      verdict `undemonstrable`, run 은 **FAILED 가 아니다**. 오늘과 같은 D 경로로 간다.

- [ ] **코드 경로는 그대로다**
      코드가 섞인 런은 `surface == "code"`, 플래너 프롬프트에 소스가 **있고**,
      contradiction 은 **여전히 verification FAILED**.

- [ ] **아무것도 안 만든 런은 프로브를 안 만든다**
      파일 0개로 끝난 런 → `outcome_demonstration` 이 `null`.
      *"증거 없음"이 "봤는데 괜찮더라"로 바뀌면 안 된다(규칙 3).*

- [ ] **비용이 늘지 않는다**
      artifact 플래너는 산문 런당 LLM 호출 **1회**. 코드 런에는 추가 호출 없음.

## 되풀이 금지

- 산출물 텍스트를 플래너 프롬프트에 넣지 마라 — [[boundary-test-must-not-supply-the-answer]]
- artifact verdict 를 gating 으로 승격하지 마라. 눈먼 플래너의 miss 는 **작업의 결함이 아니다.**
