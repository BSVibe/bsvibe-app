# E2E — 지운 파일은 검증 대상이 아니다 (실측 런 4414bcd5)

#1073 을 BSVibe 에 맡긴 실측 런 `4414bcd5`(2026-10-07): 에이전트가 루트에 스크래치 `_patch_engine.py` 를 만들고 쉘로 지웠다.
`written_paths` 는 `file_write` 만 기록하고 삭제를 모르기 때문에, gate deriver 가 `ruff check … _patch_engine.py` 를 도출했고
ruff 가 E902(파일 없음). 에이전트가 고칠 수 없는 실패로 검증 2회 → 형님께 질문하고 멈춤 (입력 358k 토큰, 15분). 형님 결정: 폐기 후 결함부터.

## 바뀐 것

- `VerificationService._still_present(box, paths)` — 1바이트 읽기로 존재 확인(`SandboxError` = 없음)
- 서버 샌드박스 `_run_derived_gate` 는 남아 있는 변경 파일만 deriver 에 넘긴다
- in-place(client_attach) 는 그대로 — 변경 목록이 형님 머신 git 에서 오고(만들었다 지운 파일은 안 나온다), 존재 확인마다 태스크 왕복이다

## 검증 (로컬)

- [x] RED → GREEN — `tests/workflow/test_the_gate_lints_only_files_that_exist.py` (지운 파일 제외 + 대조군)
- [x] 전선 절단 2곳(필터 호출 · 없음 판정) 각각 빨강
- [x] tests/execution · workflow · glue 2164 passed · ruff · mypy

## 배포 후 (prod)

- [ ] #1073 을 같은 지시로 다시 맡겨, 스크래치 파일을 지워도 검증이 E902 로 막히지 않는다
