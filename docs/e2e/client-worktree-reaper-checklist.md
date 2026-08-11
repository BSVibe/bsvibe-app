# E2E — client_attach 런이 끝나면 자기 워크트리를 돌려준다

대상: `ClientWorkerSandboxManager.release` → `worktree_reclaim_command`.
전제: `execution_target=client_attach` 제품(BStockReport-client)에 live 워커가 붙어 있음.

배경: #734 가 런마다 워크트리를 **만들기만** 한다. 이 머신에서 디스크가 차는 것은
성능 저하가 아니라 **복구 불가능한 브릭**이고, 같은 모양의 누수(#665/#666)가
`git worktree remove` 의 조용한 no-op 때문에 수개월 지속된 적이 있다.

## 라이브 검증

- [ ] **정상 종료한 런의 워크트리가 사라진다**
      `bsvibe_direct` 로 client_attach 런 투입 → 종료 후 파운더 머신에서
      `ls ~/Works/BStockReport-client/wt/` 에 그 런의 디렉터리가 **없다**.
      워커 로그에 `client_attach_worktree_reclaimed`.
      (수정 전 재현: 런마다 디렉터리가 하나씩 영구히 쌓인다)

- [ ] **작업은 남아 있다**
      같은 런의 브랜치가 살아 있다: `git -C ~/Works/BStockReport-client show run/<id>:<변경파일>`
      이 그 런의 산출물을 그대로 뱉는다. 디렉터리 회수는 브랜치도 오브젝트도 건드리지 않는다.

- [ ] **등록도 함께 정리된다**
      `git -C ~/Works/BStockReport-client worktree list` 에 그 런이 없다.
      (남으면 다음 런의 `worktree add` 가 "이미 점유된 경로"로 실패한다)

- [ ] **미커밋 작업이 있으면 회수하지 않는다** ⭐
      런을 작업 도중 취소 → 워크트리 디렉터리와 그 안의 파일이 **그대로 남는다**.
      워커 로그에 `client_attach_worktree_held reason=uncommitted_work`.
      이것이 `--force` 를 쓰지 않는 이유 전체다 — 취소된 런의 작업은 그곳에만 있다.

- [ ] **실패한 런도 회수된다**
      게이트가 REFUTED 로 끝난 런(커밋까지는 됨) → 워크트리 사라짐.
      `release` 가 `finally` 에 있으므로 happy path 뿐 아니라 모든 종료 경로를 지난다.

- [ ] **파운더의 체크아웃은 그대로**
      `~/Works/BStockReport-client` 의 브랜치·미커밋 편집·`git status` 가 런 전후로 동일.

- [ ] **머신이 안 붙어 있어도 런이 죽지 않는다**
      워커가 없는 상태에서 끝난 런이 `system_error` 가 아니라 원래 결과로 보고된다.
      로그에 `client_attach_worktree_reclaim_unreachable` 만 남는다.
      (`release` 는 `agent_loop` 의 `finally` 안이라 여기서 raise 하면 런의 결과를 덮어쓴다)

- [ ] **디스크가 실제로 줄어든다**
      회수 전/후 `du -sh ~/Works/BStockReport-client/wt` 비교. exit 0 이 "지웠다고 주장"이
      아니라 **관측**이어야 한다는 것이 #665 의 교훈이다.

## 유닛 커버 (자동)

- `tests/workflow/domain/test_client_worktree.py::TestReclaimCommand` — `--force` 금지 /
  브랜치 보존 / 디렉터리 소멸 단언 / 파운더 체크아웃 불가침.
- `tests/workflow/test_client_worktree_real_git.py` — **실 git**: 회수 / 커밋된 작업 생존 /
  미커밋 작업 보존(exit 2) / tracked 수정 보존 / 찌꺼기(`.venv`)는 회수를 막지 않음 /
  멱등 / 손삭제 후 등록 정리 / 다른 런 워크트리 불가침.
- 배선 + 음성 대조 — `release` 호출을 빼면 실패한다(확인함). 도달 불가 머신에서 raise 하지
  않음, `run_id` 없으면 아무것도 하지 않음.

## 알려진 사정거리 밖 (후속)

`release` 에 도달하지 못한 런(워커 하드 kill, 머신 리부팅)의 **고아 워크트리**는 이 변경이
치우지 않는다. 파운더 머신만으로는 "종료된 런"과 "지금 도는 런"을 구별할 수 없고 — 갓 시작한
런의 워크트리는 아직 깨끗하다 — 서버가 가진 run 상태가 필요하다. 별건.
