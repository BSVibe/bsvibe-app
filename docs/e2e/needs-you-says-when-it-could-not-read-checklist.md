# E2E — "Needs you" says when it could not read the whole list

`listPendingDecisions` reads **three independent queues** (held deliveries /
paused-run checkpoints / canon proposals) and degrades each to `[]` on its own
blip so one failure never blanks the Brief. That part is right. The defect was
what happened next: an **empty needs-you list is an answer the founder acts on**
("nothing is waiting on me"), and `NeedsYou` removed the whole section when the
list was empty. A read failure was therefore spoken as a measurement.

Same family as the `hasLiveWorker` fix (#883) — **different consequence**: that
one made two false *assertions*; this one **under-reports**. So the remedy is
different too. Items are never hidden; the *completeness claim* is what stops.

## A. 측정 (고치기 전, 유닛으로 실행)

- [x] **A-partial** — `/safemode/queue` 500 + 체크포인트 1개 존재
      → 섹션은 **렌더되고**, 칩은 **"1"**, 승인 대기 배달은 **흔적 없이 사라짐**.
      화면이 멀쩡하고 권위 있어 보인다 — 이게 위험한 쪽이다.
- [x] **A-total** — 세 큐 전부 500
      → 섹션이 **통째로 사라짐**, `placeholder`는 **false** (에러 벽도 아님).
      배달이 승인을 기다리는데 화면은 "확인할 것 없음" 이라고 말한다.

## B. 유닛/데이터 계약 (Vitest)

- [x] 한 큐만 blip → `needsYouIncomplete: true`, **읽은 항목은 그대로 남는다**
      (unknown 은 known 을 가리지 않는다)
- [x] 대조군: 세 큐 다 응답 → `needsYouIncomplete: false`
- [x] **경계**: 리뷰 **컨텍스트** 조인(runs/deliverables/products) 실패는
      `incomplete` 가 **아니다** — 항목을 빠뜨리지 않고 *제목·제품만* 못 붙인다.
      여기에 불을 켜면 사용자가 손쓸 수 없는 저하에 안내가 뜨고, 그 문구는
      "뭔가 빠졌다"는 뜻을 잃는다.
- [x] 집계 **전체**가 죽어도 `incomplete: true` (fail-closed seam, §D)

## C. 화면 (BriefContent 통과 — leaf 직접 렌더 아님)

- [x] 부분 실패: 읽은 항목 **그대로 보이고** + "일부를 못 불러왔다" 안내가 뜬다
- [x] 전체 실패: **항목이 0개여도 섹션이 남고** 안내가 뜬다 (사라지던 그 경우)
- [x] 대조군: **완전한** 빈 읽기 → 섹션은 여전히 **완전히 숨는다**
      (이게 없으면 조기 반환을 지워도 위 둘이 통과한다)
- [x] 대조군: 완전한 읽기 + 항목 있음 → 안내 **없음**
- [x] ⭐ 불완전한 읽기가 **첫 실행 온보딩을 억제하지 않는다**

### ⭐ 왜 `firstRun` 을 이 unknown 으로 막지 않았나

`firstRun` 은 `needsYou.length === 0` 을 쓰므로, 못 읽은 큐가 그 값을 **뒤집을 수
있다** — `Proposal` 에는 `run_id` 가 없다(정규화 제안은 런을 만들지 않는 지식
경로에서 나온다). 그러니 규율 119 의 판별식은 *"바뀔 수 있다"* 로 답한다.

그런데도 막지 않았다. 막으면 `/decisions` blip 하나가 **진짜 신규 사용자에게서
안내를 빼앗고**, 그건 이 화면이 닫으려던 바로 그 블로커를 거꾸로 재현하는 것이다
(#879). 대신 **unknown 을 보이게** 만들었다 — 안내와 "일부를 못 읽었다"가 **함께**
뜬다. 침묵도 거짓 주장도 아니다.

## D. Fail-closed seam (`unreadPending`)

- [x] 🚨 **전선을 끊었더니 아무도 안 잡았다.** 집계 안의 모든 읽기가 이미 자기
      ApiError 를 잡으므로 바깥 `.catch` 는 **평범한 HTTP 실패로는 도달 불가**다.
      지우지 않고 **seam 자리에서 직접 핀으로 박았다**(`vi.mock` 으로 집계를 reject).
      존재 이유: 나중에 **자기 catch 없는 읽기**가 집계에 추가되면 Brief 전체가
      `placeholder` 로 떨어져 사용자가 작업 대신 에러 벽을 본다.
- [x] 그 seam 이 **버그는 삼키지 않는다** — 비-API 예외(`RangeError`)는 전파된다

## E. 라이브 — **못 한다, 그리고 이유가 값싸지 않다**

규율 117 대로 이미 prod 를 향하는 표면을 먼저 셌다. **없다.**

증명해야 할 명제는 *"진짜 blip 때 창업자가 안내를 본다"* 인데, 그러려면
`/api/v1/safemode/queue` 를 **실제로 실패시켜야** 한다 — prod 에 결함 주입이
필요하고, 그건 이 결함이 막으려는 것보다 더 위험하다.

읽기 전용으로 prod 에서 확인 가능한 것은 **건강한 절반**뿐이다(세 엔드포인트가
살아 있고 `incomplete: false` 를 낳는다). 그건 결함과 무관하므로 적지 않는다.

- [ ] ⏭ 라이브 결함 주입 — **하지 않는다** (위 사유). 일회용 스택에서 백엔드를
      죽이는 방식이라면 가능하지만, 유닛이 이미 `BriefContent` 를 통과해 같은
      명제를 증명하므로 새 하네스를 만들 이유가 없다.

## 게이트

`tsc --noEmit` clean · biome clean · vitest 전체 green.
전선 절단 **8회** (세 큐 degrade · 바깥 seam 2분기 · 섹션 유지 · 안내 렌더 ·
BriefContent 배선 · 경계) — 각각 자기 명제만 빨강, **총 실행 수 24 불변**
(수가 변하면 문법 오류지 절단이 아니다 — 규율 121).
