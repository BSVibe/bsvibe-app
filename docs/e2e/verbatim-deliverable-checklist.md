# E2E — 산출물은 명령의 출력 그대로일 수 있다 (#673 · #1078)

BStockReport 주간 리포트: `uv run bstockreport run --emit` 이 `<<REPORT_VERBATIM>>`…`<<END>>` 사이에 숫자 블록을 찍고,
에이전트가 그 블록을 `emit_deliverable(summary=…)` 로 **다시 써서** 발행했다(프롬프트로 "고치지 말라"). 실측 09-14·09-21:
마커 줄이 텔레그램 본문에 두 번 샜고, 런 `2928e493` 은 같은 리포트를 두 번 발행했다. 숫자 파이프 한가운데의 모델은 반올림·각색,
도구 실패 시 날조도 할 수 있다.

## 바뀐 것

- `emit_deliverable(verbatim_command, verbatim_start?, verbatim_end?)` — 서버가 명령을 **런의 박스**(server_sandbox: 샌드박스,
  client_attach: 형님 머신)에서 실행해, 마커 사이 블록(마커 제외)을 출력 그대로 본문 맨 앞에, 에이전트의 `summary` 는 그 뒤 해설로
- 의심이 있으면 발행하지 않는다: 박스 없음 · exit≠0(stderr 꼬리 포함) · 타임아웃(600s) · 마커 없음 → `status: error`
- 두 트랜스포트 모두 박스를 넘긴다 — MCP(`mcp_work_effects.record_deliverable`, `shell_exec` 와 같은 박스) · 인프로세스 루프(`_drive_loop`)
- MCP 도구 스키마·설명에 필드 노출 ("도구 출력을 summary 에 다시 쓰지 말라")
- #1078: `external_ref` 없는 partial 은 **같은 런·같은 내용**이면 다시 발행하지 않는다

## 검증 (로컬)

- [x] RED → GREEN — `tests/workflow/test_a_verbatim_deliverable_is_the_commands_own_output.py` (RED 9 + 대조군)
- [x] 전선 절단 6곳(verbatim 분기 · exit 검사 · 마커 추출 · 내용 중복 · MCP 박스 · 루프 박스) 각각 빨강
- [x] 실제 쉘(NoopSandbox)로 마커 블록 추출(선행 공백 보존) · exit 3 거부 확인
- [x] workflow · glue · mcp · delivery · execution 2686 passed · ruff · mypy · lint-imports

## 배포 후 (prod)

- [ ] BStockReport 스케줄 지시를 `verbatim_command` 사용으로 바꾼다 (형님 결정 — 스케줄 내용 변경)
- [ ] 다음 주간 런: 텔레그램 본문에 마커 없음, 숫자 블록이 `--emit` 출력과 바이트 동일, 발행 1회

## 남은 틈

- 터미널(verified) 산출물 경로는 그대로 — 에이전트가 쓰는 요약. verbatim 은 mid-run emit 으로만
