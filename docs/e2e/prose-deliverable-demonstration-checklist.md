# E2E — 산문·데이터 산출물도 증거를 벌 수 있다

대상: `_run_outcome_demonstration` 의 artifact 표면 + `artifact_planner_messages`
+ `summarize(contradiction_fails=…)` + `drop_unasserted`. 검증 설계 SoT §8 (트랙 C1/C1b).

배경: 게이트 파생기는 산문 산출물을 *"the judge and demonstration paths cover it"* 이라며
`applicable: false` 로 비켰는데, demonstration 경로는 `_is_code_path` 가 아니면 `None` 을
냈다. **아무도 안 맡아서** 비개발 산출물은 정의상 등급 D 였고 매니페스트 있는 레포에선
매번 사람을 불렀다. I2 기계 자체는 이미 표면 중립이었다 — 막던 건 입구 하나.

⚠️ 이 확장의 급소는 §4 규칙 1 이다. 산출물 텍스트를 본 플래너는
`grep "<방금 읽은 문구>"` 를 쓴다 — **모든 산문이 자동 통과**. 그래서 artifact 플래너는
텍스트에 **눈이 멀어** 있고, 그 판정은 **advisory**(벌 수만 있고 떨어뜨릴 수 없음)다.

## 라이브 검증

- [x] **산문 산출물이 등급 D 를 벗어난다** ⭐
      ✅ run `e72689e8` (2026-08-14, prod `6a1143e`): `passed / grade B / gate_expected=true`,
      `execution_decisions` **0건**, 런은 `review_ready` 로 종료.
      대조군 `0bbf72eb`(같은 레포·같은 성격, C1 이전): `grade D` ×2 +
      `weak_evidence_no_gate` ×2 + `outcome_demonstration` 이 `null`.
      **`gate_expected` 는 양쪽 다 `true`** — 같은 조건에서 결과만 바뀌었다.

- [x] **artifact 프로브가 실제로 돈다**
      ✅ 프로브 6개 실행, `surface="artifact"`, 한 런의 4개 라운드에서 연속 재현.
      가장 강한 프로브는 문서가 언급한 `.py` 경로를 뽑아 `os.path.exists` 로 대조했다 —
      **산출물 바깥의 것과 맞춰본 것**이라 §8.3 이 요구한 모양이다.

- [x] **못 맞춰도 실패시키지 않는다** ⭐
      ✅ 중간 라운드에서 검증이 FAILED 했지만 원인은 **judge**(문서가 잘려 D 등급 조건과
      "사람 검토 vs 자동 진행" 절이 누락)였고, 그 라운드에서도 demonstration 은
      `demonstrated` 였다. artifact 표면은 떨어뜨리지 않는다.

- [~] **플래너가 자기가 채점할 텍스트를 못 본다** ⭐⭐
      ⚠️ **프로덕션 로그로는 증명 불가** — 플래너 프롬프트는 어디에도 기록되지 않는다.
      prod 가 증명하는 것은 `surface="artifact"`, 즉 **눈먼 경로가 탔다**는 사실까지다.
      눈이 멀었다는 것 자체는 **구조**(`artifact_planner_messages` 가 경로만 받는다)와
      유닛 테스트가 보증한다. 그 이상으로 말하면 과장이다.
      간접 증거는 있었다 — 프로브가
      `grep -qiE '사람|human|수동.{0,4}검토|escalat|검토자|reviewer'` 처럼 **표현 변형을
      늘어놓았다.** 본문을 읽었다면 그 문장을 그대로 grep 했을 것이다.

- [ ] **코드 경로는 그대로다**
      코드가 섞인 런은 `surface == "code"`, 플래너 프롬프트에 소스가 **있고**,
      contradiction 은 **여전히 verification FAILED**.

- [ ] **아무것도 안 만든 런은 프로브를 안 만든다**
      파일 0개로 끝난 런 → `outcome_demonstration` 이 `null`.
      *"증거 없음"이 "봤는데 괜찮더라"로 바뀌면 안 된다(규칙 3).*

- [ ] **비용이 늘지 않는다**
      artifact 플래너는 산문 런당 LLM 호출 **1회**. 코드 런에는 추가 호출 없음.

## 🔴 실증이 잡은 결함 — 단언 없는 프로브 (C1b)

run `e72689e8` 의 프로브 6개가 **전부** `expect_stdout_contains` 를 비워두고 exit code 에만
의존했고, 그중 **둘은 실패할 수가 없었다**:

```
python -c "...; print(''.join(sorted(set(g for g in 'ABCD' if g in c))))"
python -c "...; print('found' if ex else 'missing')"
```

문서에 A/B/C/D 가 하나도 없어도 빈 문자열을 찍고 exit 0 → `matched`.
**답을 계산해놓고 버린다.** 6개 중 2개가 무조건 통과하는 프로브였고, 등급은 그걸 구분 못 했다.

∴ artifact 표면에서는 **단언 없는 프로브를 증거로 안 친다**(`drop_unasserted`) — 실행 전에
버리고, 남는 게 없으면 정직하게 `undemonstrable`. 벌 수만 있는 표면이라 fail-open 이 아니다.

- [ ] **단언 없는 프로브가 사라졌다** ⭐
      다음 산문 런의 `probes[]` 가 전부 `expect_stdout_contains` 를 갖는다.
      프로브 수가 줄어도 등급은 유지된다(진짜 프로브가 하나라도 matched 면 `demonstrated`).

- [ ] **버려도 어디를 봤는지는 남는다**
      전부 버려진 런의 blob 이 `verdict=undemonstrable` + `surface="artifact"`.
      (C1b 이전엔 빈 계획 경로에서 `surface` 가 아예 빠졌다 — run `e72689e8` 4번째 행.)

⚠️ **코드 표면에는 같은 조임을 적용하지 않았다.** 거긴 demonstration 이 **게이팅**이라
프로브를 버리면 통과가 사람 호출로 바뀐다 — 이 트랙이 줄이려는 바로 그것. 별건 판단.
(`test_the_code_surface_keeps_its_unasserted_probes_for_now` 가 이 결정을 고정한다.)

## 되풀이 금지

- 산출물 텍스트를 플래너 프롬프트에 넣지 마라 — [[boundary-test-must-not-supply-the-answer]]
- **단언 없는 프로브는 증거가 아니다.** `print(...)` 는 답이 틀려도 exit 0 이다.
- artifact verdict 를 gating 으로 승격하지 마라. 눈먼 플래너의 miss 는 **작업의 결함이 아니다.**
