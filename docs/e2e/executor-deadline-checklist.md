# E2E 체크리스트 — 디스패치 데드라인 전파 (#965)

**대상**: `dispatch_task(timeout_s=...)` · `ExecutorAdapter` 배선 · 워커의 `_task_deadline_s` / `_with_deadline`

## 기계적 검증

- [x] `uv run pytest tests/executors/test_dispatch_deadline_reaches_the_worker.py` — 양 끝 + 배선
- [x] 전선 절단: 어댑터에서 `timeout_s=` 를 빼면 배선 테스트가 **"디스패치가 데드라인을 안 실었다"**로 빨개짐
      (복원 후 해시 재검증)
- [x] 워커 테스트는 **취소까지** 단언한다 — 데드라인이 지나도 서브프로세스가 살아 있으면 슬롯이 안 돌아온다
- [x] `tests/executors` + `tests/dispatch` 572 passed

## 배포 후 prod 에서 볼 것

> ⚠️ **호스트 워커는 autodeploy 되지 않는다.** `launchctl kickstart -k gui/501/com.bsvibe.worker{,-admin,-mac-mini-e2e}`
> 후 **프로세스 시작 시각**으로 새 코드인지 확인할 것. 백엔드만 새 코드면 payload 에 `timeout_s` 가
> 실려도 워커가 그것을 **읽지 않는다**(구버전 워커 = 자기 데드라인 유지, 안전하지만 이 PR 의 효과는 0).

- [ ] **프레이밍이 행 나도 워커가 300초쯤에 스스로 접는다** — 워커 로그에 `task_deadline_exceeded`,
      이어서 `task_completed success=False`. 그 태스크 행은 `failed` 로 닫힌다(`dispatched` 고착 아님)
- [ ] **슬롯이 돌아온다** — 위 직후 워커가 다시 poll 하고 다음 태스크를 `task_received` 한다.
      (이번 사건의 핵심 증상: 8분간 슬롯이 물려 있었고 그동안 아무것도 못 받았다)
- [ ] **긴 act 턴은 안 잘린다** — `agent_loop.act` 는 `default_timeout_s=None` 이라 3600s 가 실린다.
      정상 코딩 런 하나가 5~15분 걸려도 중간에 끊기지 않는지 확인. **이게 회귀 위험이 가장 큰 지점이다.**

## 이 PR 이 하지 않는 것

- [ ] **잃어버린 태스크의 재전달** — poll 이 XREADGROUP 후 **읽는 즉시 ACK** 하므로, 워커에 닿지 못한
      엔트리는 영영 사라진다(2026-09-15 관측: 워커가 뒤 엔트리인 *cancel* 은 받았는데 그 앞의 태스크
      엔트리는 못 받음 ⇒ 그룹 커서가 이미 지나갔다). 고치려면 **"워커가 받았다"는 신호**가 따로 있어야
      한다 — 단순 XAUTOCLAIM 재전달은 **정상적으로 오래 도는 act 턴을 중복 실행**시킨다(LLM 비용 2배).
      #965 에 설계 메모를 남겼다.
