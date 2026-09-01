# E2E — exec 타임아웃 메시지가 자기 실제 예산을 말한다

**왜** — 2026-09-01, `#866` CI 실패가 이렇게 찍혔다:

```
exec timed out after 10.0s (36 polls in 70.0s, last status 'dispatched')
```

`10.0s` 는 awaiter 의 예산이 **아니다**. awaiter 는 `timeout_s + _AWAIT_SLACK_S`
= 10 + 60 = **70.0s** 를 쓴다. 그래서 예산을 다 쓴 **정상** 타임아웃이 "7배 초과"로
읽혔고, 그 오독이 진단을 *"굶은 CI 러너"* 로 보냈다. 7이라는 비율은 부하와 무관하게
`_AWAIT_SLACK_S` 때문에 **항상** 나오는 상수다.

`36 polls / 70.0s` 는 `_AWAIT_POLL_INTERVAL_S=2.0` 기준 기대치(35)와 일치하는
**건강한** 값이고, `dispatch.TaskTimeout` docstring 은 기아를 *"polls 가 그 비율보다
훨씬 아래"* 로 정의한다 — 관측된 것은 정반대. 실제 해당하는 항목은 첫 번째,
**"워커가 보고하지 않았다"** 다.

## 체크리스트

- [ ] 무부하 로컬에서 **워커가 태스크를 안 집어가게** 두고 `exec(timeout_s=10.0)` 을
      돌리면, 옛 문자열(`after 10.0s ... 70.0s`)이 **재현된다** — 즉 그 시그니처는
      부하의 증거가 아니다 (수정 전 기준선)
- [ ] 수정 후 같은 조건에서 `after` 뒤의 숫자가 **경과 시간 이상**이다
- [ ] 메시지가 두 예산을 **구분해서** 말한다 — 커맨드 자신의 예산과 보고 슬랙
- [ ] `polls` · `last_status` 는 그대로 실려 있다 (#821 계약 유지)
- [ ] `_map_result` 의 `"exec timed out"` **접두사 계약이 깨지지 않는다**
      (그 접두사를 읽는 쪽은 워커측 `worker/main.py` 메시지지만, 접두사는 유지한다)
- [ ] structlog `client_worker_exec_timeout` 이벤트에 `awaiter_budget_s` 가 실린다

## 재현 (수정 전 기준선을 다시 보고 싶을 때)

워커를 seed 하되 스트림을 **아무도 소비하지 않게** 두고 `box.exec(..., timeout_s=10.0)`.
70초 뒤 타임아웃한다. 무부하에서도 나온다 — 그게 요점이다.
