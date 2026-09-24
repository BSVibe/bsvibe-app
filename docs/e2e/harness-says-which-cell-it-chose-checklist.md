# E2E — 하네스가 어느 칸을 골랐는지 말한다 (#950 클레임 경로 계측)

**대상 PR**: `tests/supervisor/sandbox/test_client_worker_manager.py` 의 가짜 워커와
판별자가 structlog 행을 남긴다 (`harness_worker_*`)
**이슈**: #950 (판정표) · 선행: #1040 (CI 구조화 로그 아티팩트)

> 📏 **이 변경은 제품 코드를 한 줄도 안 건드린다.** 고친 것은 *"CI 빨강을 읽을 수
> 있는가"* 뿐이다. 그러니 이 체크리스트의 1순위는 "기능이 되나"가 아니라
> **"다음 빨강에서 칸이 갈리나"** 다 — 그리고 그건 **CI 에서만** 닫힌다.

---

## 왜 지금 이게 필요한가 — 2026-09-23 빨강의 모순

| 증거 | 말하는 것 |
|---|---|
| 실패 문구가 **제품 쪽 단언**(`assert result.timed_out is False`) | `await worker` 도 `_attribute_a_late_worker` 도 안 던졌다 ⇒ 판별자는 **"워커가 제때 기록했다"** 로 판정 |
| 아티팩트에 그 태스크의 `executor_task_result_recorded` **없음**, `last_status` 70초 내내 `dispatched` | 기록은 **없었다** |

둘은 양립 못 한다. 가릴 수 없었던 이유는 하나다 — **판별자는 실패할 때만 말한다.**
통과 판정은 조용히 `return` 하고 아무 행도 안 남긴다.

---

## 로컬 실측 (완료)

- [x] **RED 먼저** — 계측 없는 상태에서 9건 전부 "행이 없다"로 실패
      (`ImportError` 아님: 마지막 둘은 실제 exec 경로를 끝까지 돈 뒤 단언에서 실패)
- [x] **행이 아티팩트 파일에 실제로 닿는다** — `capture_logs` 가 아니라
      `var/test-logs/pytest-structlog.jsonl` 을 직접 세었다

      | 이벤트 | 개수 (sandbox 스위트 1회) |
      |---|---|
      | `executor_task_dispatched` | 12 |
      | `harness_worker_saw_task` | **7** |
      | `harness_worker_recorded` | **7** |
      | `harness_worker_attribution` | **10** |
      | `harness_worker_gave_up` | **1** |

      ⇒ 디스패치 12건 중 하네스가 집은 것은 **7건**. 나머지 5건은 `_worker_then` 을
      안 쓰는(일부러 타임아웃시키는) 테스트다 — **이 대조가 아티팩트만으로 보인다**
- [x] **조인이 된다** — `task_id` 로 제품 이벤트와 하네스 이벤트가 붙는다.
      "하네스가 봤는데 제품 기록 없음" = **0건**(오늘 초록 런 기준 정상)
- [x] **합성 판정이 살아있는 판정을 오염시키지 않는다**

      | | 개수 | 판정값 |
      |---|---|---|
      | 살아있음(`task_id` 있음) | 7 | `worker_kept_up` 만 |
      | 합성(핀 유닛테스트) | 3 | `harness_recorded_late` · `harness_never_recorded` 포함 |

      ⇒ 집계 규칙은 한 줄이다: **`task_id` 없는 판정 행은 살아 있는 판정이 아니다.**
      이걸 구조로 만든 이유는 `capture_logs` 를 기억하는 규율에 기대지 않기 위해서다
- [x] **셋째 칸(다섯째 칸)을 일부러 만들어 걸었다** — 워커는 정상으로 두고 awaiter 만
      타임아웃시켰다. 이 칸은 지금까지 **한 번도 실증된 적이 없다**

      | | |
      |---|---|
      | 첫 판은 **절단해도 안 뒤집혔다** | `gave_up_at` 을 `await worker` **뒤에** 재서 *"제때 기록했다"* 가 구조적으로 항상 참이었다 — 테스트가 자기가 만든 답을 확인하고 있었다 |
      | 고친 뒤 절단 | **이웃 칸으로 뒤집힌다**(`harness_recorded_late` = "러너가 느렸다") ⇒ 이 테스트는 실제로 두 칸을 가른다 |

      ⇒ 절단할 때마다 **10개 실행**(컴파일 확인). 안 뒤집히는 방어였다면 지웠을 것이다
- [x] `ruff check` · `ruff format --check` · sandbox 스위트 **99 passed**

---

## CI 에서만 닫히는 항목 — ⚠️ **머지했다고 닫지 마라**

- [ ] **다음 CI 빨강의 아티팩트에서 이 행들이 보인다.**
      `pytest-structlog` 아티팩트를 받아 `harness_worker_attribution` 을 세고,
      `task_id` 가 있는 행만 남긴 뒤 flake 태스크의 id 로 조인한다
- [ ] **그 빨강에서 칸이 갈린다** — 아래 셋 중 어느 것인지 **한 번의 조회**로 나와야 한다

      | 아티팩트가 보이는 것 | 판정 |
      |---|---|
      | 그 `task_id` 에 `harness_worker_saw_task` **없음** | 하네스가 태스크를 **못 받았다** — 수신/클레임 실패 |
      | `saw_task` 있고 `harness_worker_recorded` 없음 | 하네스가 받고 **서브프로세스에서 멈췄다** |
      | `recorded` 있고 `verdict=worker_kept_up` 인데 제품이 타임아웃 | **제품 쪽 공백** — 기록이 커밋됐는데 awaiter 가 못 봤다 (판정표 다섯째 칸) |
- [ ] **음성 대조군을 같은 창에서** — 같은 아티팩트에 `executor_task_dispatched` 가
      있는지 먼저 확인한다. 생산자가 꺼져 있으면 그 0 은 *"안 일어났다"* 가 아니라
      *"잴 수 없다"* 다 (이 이슈가 두 번 속은 모양)

> 🧭 **이 체크리스트의 마지막 세 칸은 빨강을 기다린다.** flake 는 재현 명령이 없다 —
> 그래서 *"고쳤다"* 가 아니라 *"다음에 읽을 수 있게 했다"* 가 이 PR 의 주장이다.
