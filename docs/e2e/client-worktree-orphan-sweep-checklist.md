# E2E — 죽은 런이 남긴 워크트리를 다음 런이 회수한다

대상: `ClientWorkerSandboxManager._sweep_orphan_worktrees` (acquire 시점).
전제: client_attach 제품에 live 워커.

배경: #736 은 `release`(= `agent_loop` 의 `finally`)에서 회수한다. 프로세스가 **kill** 된 런은
거기 도달하지 못하고, 레포 전체 체크아웃이 파운더 디스크에 남는다. 이 머신에서 디스크가 차는
것은 복구 불가능한 브릭이다.

**판단은 서버가, 목록은 머신이.** 파운더 머신만으로는 버려진 워크트리와 아직 도는 런의 것을
구별할 수 없다 — 읽기만 한 런의 워크트리는 깨끗하고 젊어서 한 시간 전에 죽은 런의 것과 똑같다.

## 라이브 검증

- [ ] **죽은 런의 워크트리가 다음 런에서 사라진다** ⭐
      런을 하나 태우다 워커 프로세스를 kill → `wt/<short>` 가 남는다.
      워커 재기동 후 **다음 런**을 태우면 그 디렉터리가 사라진다.
      로그 `client_attach_orphan_worktree_reclaimed orphan=<short>`.

- [ ] **도는 런의 워크트리는 그대로**
      두 런을 동시에 태운다. 나중 런의 acquire 가 앞 런의 워크트리를 지우지 않는다
      (앞 런은 `RUNNING`). 앞 런이 정상 완료된다.

- [ ] **미커밋 작업을 쥔 고아는 남는다** ⭐
      작업 도중 kill → 그 워크트리에 미커밋 파일이 있다. 다음 런의 sweep 이 **못 지운다**.
      로그 `client_attach_orphan_worktree_kept exit_code=2`. 파일 그대로.

- [ ] **파운더 자신의 워크트리는 건드리지 않는다**
      `<repo>/wt/` 밖(예: `~/Works/BStockReport-client-wt/...`)의 워크트리와 메인 체크아웃이
      목록에도 안 잡히고 지워지지도 않는다.

- [ ] **머신이 안 붙어 있어도 런이 시작된다**
      워커가 없는 상태에서 sweep 이 실패해도 `acquire` 는 성공한다
      (로그 `client_attach_worktree_sweep_failed`만).

- [ ] **디스크가 실제로 줄어든다**
      고아 몇 개를 만들어 둔 뒤 `du -sh <repo>/wt` 를 sweep 전후로 비교.

## 유닛 커버 (자동)

- `tests/workflow/test_orphan_worktree_sweep.py` — 죽은 런 회수 / `RUNNING`·`OPEN` 보호 /
  자기 워크트리는 status 와 무관하게 제외 / sweep 실패가 런을 못 죽임.
  음성 대조 2건 확인(호출 제거 → 회수 안 됨, status 무시 → live 런 삭제).
- `tests/workflow/test_client_worktree_real_git.py` — **실 git**: 목록이 `<repo>/wt/` 것만
  잡고 파운더 워크트리를 제외 / 고아는 회수되고 미커밋을 쥔 것은 exit 2 로 남는다.
- `tests/workflow/domain/test_client_worktree.py::TestOrphanSweep` — porcelain 파싱,
  `--force` 금지, 소멸 단언.

## 알려진 사정거리 밖

제품이 **조용해지면** 고아가 그대로 남는다 — sweep 은 다음 런이 시작될 때 돈다. 검증 슬롯
리스(#725)와 같은 성질이고 같은 이유로 받아들인다: 별도 리퍼 워커와 그 스케줄을 살려두는
비용이 더 크다.
