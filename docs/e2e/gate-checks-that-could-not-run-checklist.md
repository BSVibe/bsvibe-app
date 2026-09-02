# E2E — 못 돌린 게이트 검사를 산출물이 말한다

`backend/workflow/application/_verified_summary.py`.
파생 게이트에서 **도구가 없어 못 돌린 명령**(exit 127 → `unavailable`)이 산출물
요약에서 사라지던 것을 고친다.

## 왜 조용했나 — `unavailable` 은 게이트를 깨지 않는다

```python
# verification_service.py — 게이트 판정
passed = not any(r["status"] == "failed" for r in results)   # unavailable 은 실패가 아니다
...
command_gate_pass = bool(derived_gate["passed"])             # → 런은 PROVED 에 도달한다
```

그리고 요약 문장은 **통과한 것만** 셌다:

```python
passed = [c for c in commands if c.get("passed")]
pieces.append(_gate_command_sentence(passed, commands, ko))   # "검증: 3개 확인 통과."
```

⇒ 5개짜리 게이트 중 2개가 못 돌아도 **3개짜리 게이트가 전부 통과한 것과 글자
그대로 똑같이 읽힌다.** 참이면서, 중요한 한 방향으로 불완전하다.

실측 (2026-09-02, prod 가 내는 모양 그대로):

| | 고치기 전 | 고친 뒤 |
|---|---|---|
| KO | `검증: 3개 확인 통과.` | `검증: 3개 확인 통과. 여기서 못 돌린 검사 2개(도구 없음): docker compose -f deploy/compose.yaml config · uv run lint-imports.` |
| EN | `Verified: 3 checks passed (tests, lint, types).` | `… 2 checks could not run here (tool missing): docker compose … · uv run lint-imports.` |

⚠️ **왜 "(도구 없음)" 을 같이 적나** — 안 돈 검사를 *깨진* 검사로 읽으면 안 된다.
exit 127 은 그 머신에 도구가 없다는 뜻이지 코드가 틀렸다는 뜻이 아니다.

⚠️ **왜 별도 줄이 아니라 같은 문장 뒤인가** — `_shipped_detail` 은 `검증`/`Verified`
프리픽스로 **한 줄만** 들어올린다. 자기 줄에 놓인 절은 형님 폰에 영원히 안 간다
(약한증거 문장이 같은 이유로 프리픽스를 유지한다 — 교훈 #742).

## 체크리스트

- [x] 유닛: `uv run pytest tests/execution/test_run_orchestrator.py -q -k "could_not_run or stays_silent_when_every"` → 4 passed
- [x] **RED 실증** — 구현을 되돌리면 3개가 죽는가 (기능 부재)
- [x] **대조군 실증** — `_could_not_run_clause` 가 *항상* 절을 내도록 바꾸면
      `test_summary_stays_silent_when_every_gate_check_ran` 이 죽는가
      (이게 없으면 "언제나 덧붙이는" 구현이 나머지 테스트를 전부 통과한다)
- [x] 회귀: 문장을 고정하는 인접 스위트 4개가 초록인가
      (`test_run_orchestrator` · `test_verification_feedback` ·
      `test_client_attach_lands_its_work` · `test_shipped_producer`)
- [x] KO 에 영어 크롬이 안 샌다 (`could not run` / `Verified` 부재)
- [x] 전부 통과한 게이트는 **아무것도 얻지 않는다** (문장이 그대로다)
- [x] `_shipped_detail` 이 절까지 들어올린다 (폰 알림에 도달)
- [x] 전체 게이트 5종 초록 — pytest 6578 passed / 91.01% · ruff · ruff format · lint-imports 5 kept 0 broken · mypy 579 clean

## 라이브 확인 (다음에 `unavailable` 이 실제로 나올 때)

이 조건은 **샌드박스에 없는 도구를 게이트가 파생할 때** 나온다 — 실측 사례는
런 `7c1bb4a2` 의 회고 노트(*"이 sandbox 실행 환경에는 docker 바이너리가 없다"*).

- [x] 그런 런의 deliverable `payload.summary` 에 `못 돌린 검사` 절이 있는가
      → 런 `81a168ed` (2026-09-02): *"여기서 못 돌린 검사 2개(도구 없음): bash -c
      '… docker compose … config …' · bash -c '… compose.e2e-live … config …'"*.
      못 돌린 그 둘이 하필 **문서의 숫자가 맞는지** 확인하는 명령이었다.
- [x] 같은 절이 형님 폰 알림(`shipped` detail)에도 도달했는가
      → `notification_events` 의 그 런 `shipped` 행 body 에 그대로 있다.
      ⚠️ **그리고 그게 결함을 하나 드러냈다** — 명령을 원문으로 이름 대는 바람에
      알림 본문이 ~700자가 됐다. #872 이전엔 `검증: 4개 확인 통과.` 로 끝났으므로
      **#872 이 넣은 회귀**다. 명령 이름을 72자로 자르고 컷을 표시하도록 고쳤다
      (`_clip_command`) — 560자 → 140자, 식별성은 유지.
