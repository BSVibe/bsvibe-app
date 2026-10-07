# E2E — 자동 squash 머지는 PR 제목을 main 에 남긴다 (#1143)

실측 2026-10-07: PR #1142 제목은 *"라우팅 계정 누락 시 런 멈춤 수정"* 인데, 머지 감시의 자동 squash 가 main 에 남긴 커밋은
`work: (재시도 — 이전 런 4414bcd5 은 …) (run-b2ebd3c4) (#1142)` (`2fbd0da`). 머지 요청이 `{"merge_method": "squash"}` 만 보냈고,
커밋 하나짜리 PR 의 GitHub 기본 squash 제목은 그 커밋 메시지 — 런 커밋은 지시문 첫 줄이다.

## 바뀐 것

- `GithubClient.merge_pr(..., commit_title=None)` — 주면 `commit_title` 을 함께 보낸다 (없으면 본문 그대로)
- `MergeWatchWorker._merge` — 락 아래 재확인한 PR 의 `title` 로 `"<제목> (#<번호>)"`. 제목이 없으면 GitHub 기본 그대로

## 검증 (로컬)

- [x] RED → GREEN — `tests/workflow/infrastructure/test_squash_merge_keeps_the_pr_title.py` (RED 2 + 대조군 2)
- [x] 전선 절단 2곳(워커가 제목 전달 · 클라이언트가 본문에 싣기) 각각 빨강
- [x] 머지 감시 테스트 55 passed · ruff · mypy

## 배포 후 (prod)

- [ ] 다음 BSVibe PR 이 자동 머지되면 main 커밋 제목이 PR 제목 + ` (#번호)`

## 남은 틈

- 런 커밋 메시지 자체(`work: <지시문 첫 줄>`)는 그대로다 — PR 브랜치 기록에는 남는다. main 에는 이제 안 남는다
