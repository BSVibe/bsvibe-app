# E2E — 웹훅 라우트가 Receive 의 라우팅 키를 실제로 심는다

형님 결정 (2026-09-12): **배선한다(지우지 않는다).**

`receive()` 는 `connector_account_id` + `resource_id` 두 payload 키로 바인딩을
찾고, 그 **아래에서** `trigger.filters` 를 적용한다. prod 실측(읽기 전용):

```sql
select count(*),
       count(*) filter (where payload::jsonb ? 'connector_account_id'),
       count(*) filter (where payload::jsonb ? 'resource_id')
from trigger_events where trigger_kind='webhook';   -- 13 | 0 | 0
```

**13 건 중 0 건.** 그 분기는 prod 에서 한 번도 돌지 않았고, 따라서 `filters` 도
한 번도 적용된 적이 없다 — MCP `bsvibe_bindings_*` 와 REST 로 **오늘도 설정
가능한데도**. PR #924 는 형제 노브 `trigger.enabled` 를 *"Receive 는 filters 만
소비한다"* 는 근거로 지웠는데, 그 문장이 가리킨 대상이 inert 였다.

> ⚠️ **이 문서의 `- [ ]` 는 "아직 안 걸어봤다"는 뜻이다** — "제품이 못 만든다"가
> 아니다. 아래 라이브 항목은 전부 prod 데이터로 걸어볼 수 있는 상태다.

---

## A. 유닛 / 글루 — 자격증명 없이 돈다

`tests/glue/test_receive_routing_keys_wired.py` (신규 8건, 프로브 PG 에서 실행)

- [x] 바인딩이 잡힌 배송의 **저장된** payload 에 두 키가 다 실린다
      (`test_a_bound_delivery_stores_both_routing_keys`)
- [x] 커넥터가 숫자로 보낸 값도 **문자열**로 심긴다 — 텔레그램 `chat_id` 는 JSON
      number, 바인딩 행은 `"8242700007"`. 캐스트를 잃으면 한 홉 뒤에서 똑같이
      조용히 실패한다
- [x] 바인딩이 **안 잡히면** 두 키 다 안 심긴다 — 오늘의 pass-through 유지
      (`test_an_unbound_delivery_stores_neither_routing_key`)
- [x] ⭐ **두 반쪽이 만난다**: 라우트가 실제로 저장한 행을 꺼내 `receive()` 에
      먹이면 바인딩 분기에 도달한다(`binding_id` · `selection` 보강)
      (`test_the_stored_event_reaches_the_binding_branch_in_receive`)
      — 기존 테스트는 전부 **한쪽 반만** 직접 호출했고, 그래서 이 구멍이 살았다
- [x] 안 맞는 필터가 이벤트를 **떨어뜨린다** (`filtered_out=True`,
      `reason="filter_rejected"`)
- [x] 맞는 필터는 통과시킨다 — 양성 대조군. 이게 없으면 "전부 거절" 구현도
      거절 테스트를 통과한다
- [x] **빈 필터는 전부 통과** — prod 무회귀 가드 (두 바인딩 다 `{"filters": {}}`)
- [x] 두 끝이 키 이름을 **한 정의**에서만 읽는다 (`backend/shared/wire_kinds.py`)
      + 파일 집합이 비어 있지 않다는 동반 단언

**전선 절단 실증** — 5회, 전부 `py_compile` 통과 확인, 복구는 바이트 스냅샷 +
재해시. 기준선 = 이 모듈 8건 실행.

| 끊은 것 | 빨강 | 초록 유지 |
|---|---|---|
| 라우트가 바인딩만 찾고 키를 안 심음 (= PR 전 동작) | 5 | 3 |
| 로컬 dict 에만 심고 저장은 `event.payload` 로 | 5 | 3 |
| `str()` 캐스트 상실 — 원시 숫자를 심음 | 5 | 3 |
| 바인딩 미매치에도 **무조건** 심음 | 1 (미바인딩 가드) | 7 |
| 읽는 쪽이 키 이름을 로컬 리터럴로 다시 적음 | 1 (단일정의 가드) | 7 |

## B. 라이브 — prod 에서 형님이 걸어본다

- [ ] 바인딩된 텔레그램 1:1 채팅에 메시지를 보낸다 → 새 `trigger_events` 행의
      `payload` 에 `connector_account_id` + `resource_id` 가 실린다
      ```sql
      select payload->>'connector_account_id', payload->>'resource_id'
      from trigger_events where trigger_kind='webhook'
      order by received_at desc limit 1;
      ```
- [ ] 같은 행에서 `product_id` 는 여전히 세팅돼 있다 (#922 의 축은 그대로)
- [ ] 런이 정상 생성된다 — `filters` 가 `{}` 이므로 아무것도 안 떨어진다
- [ ] **바인딩 없는** 그룹 채팅에 메시지를 보낸다 → 그 행의 payload 에는 두 키가
      **없다** (pass-through 유지)
- [ ] MCP `bsvibe_bindings_update` 로 `trigger.filters` 를 안 맞는 값으로 걸고
      메시지를 보낸다 → `trigger_events.payload` 에 `_received_filtered` 가
      찍히고 Request 는 안 생긴다. **확인 후 `{}` 로 되돌린다**

## C. 안 건드린 것 (의도)

- 파서 5종(telegram · discord · slack · github · sentry) — 그대로. 충돌 검사
  결과 어느 파서도 `resource_id` / `connector_account_id` 이름의 payload 필드를
  쓰지 않는다
- `trigger_events.product_id` — #922 가 세팅하고 인덱스도 걸려 있다. 안 옮기고
  안 지웠다
- 라우트의 product 해석 순서 — 파서 → 형님의 명시적 바인딩 → repo 추론
- `resource_bindings.trigger` 의 **DB 컬럼 기본값**은 아직
  `{"enabled": false, "filters": {}}` 다. #924 는 파이썬 `_default_trigger()` 만
  고쳤다. 아무도 `enabled` 를 안 읽으므로 무해하지만, 기록해 둔다
