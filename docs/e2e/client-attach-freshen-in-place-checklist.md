# E2E — stale 해진 client_attach PR 이 파운더 머신에서 최신화된다

대상: `BranchFreshener` 시임 + `freshen_on_client_machine` + `build_branch_freshener` 라우팅.
전제: client_attach 제품(BStockReport-client)에 live 워커, active github 커넥터,
`github_auto_merge_enabled=true`, `worker_mode=redis_streams`(exec 채널).

배경: 머지워치는 PR 을 머지 가능하게 유지하려고 base 를 런 브랜치에 머지해서 push 한다.
그걸 **서버측 클론**에서 했다 — client_attach 제품이 절대 가질 수 없는 것(§3.5). 그래서
#738 은 정직하게 멈췄고, 그런 PR 은 아무도 모르는 채 사람을 기다렸다.

멈춤은 옳았고 **결론이 너무 넓었다** — #692 가 게이트에 대해 이미 한 번 바로잡은 그 모양이다.
*어디서 도느냐*는 애초에 질문이 아니었다. `git merge` 의 exit code 가 clean/conflict 를 정하고,
그 답은 체크아웃을 가진 어느 머신에서나 같다.

## 라이브 검증

- [ ] **stale PR 이 스스로 최신화된다** ⭐
      client_attach 런의 PR 이 열린 뒤 base(main)에 다른 커밋을 넣어 `behind` 로 만든다 →
      다음 폴에서 워커 로그에 `freshen_in_place_freshened branch=run/<run8> base_branch=main`,
      github 의 PR 이 "up to date" 로 바뀌고 CI 가 새 head 로 다시 돈다.
      (수정 전 재현: `merge_watch_freshness_skipped_client_attach` → 행이 FAILED)

- [ ] **서버에는 아무것도 안 생긴다** ⭐
      `docker --context colima exec bsvibe-prod-backend-1 ls /app/var/runs/<run_id>` 가
      비어 있거나 없다. 로그에 `merge_watch_reclone` 이 **없다**.

- [ ] **파운더 머신에 토큰이 안 간다** ⭐
      `executor_tasks.prompt` 의 그 런 exec 명령들에 `https://`·`x-access-token` 이 없다.
      fetch/push 는 형님 머신의 기존 git 자격으로 된다.
      ```sql
      select prompt from executor_tasks where prompt like 'git %' order by created_at desc limit 8;
      ```

- [ ] **워크트리가 없어도 된다** ⭐
      런이 끝나 #736 이 워크트리를 회수한 **뒤**에 stale 을 만든다 → 최신화가 그래도 된다
      (프로비저닝이 브랜치에서 워크트리를 다시 만든다). `git -C ~/Works/BStockReport-client
      worktree list` 로 생겼다가 사라지는 것 확인.

- [ ] **진짜 conflict 는 에이전트에게 간다**
      base 와 런 브랜치가 **같은 줄**을 다르게 고치게 만든다 → `freshen_in_place_conflict`
      + `merge_watch_conflict_dispatched` → 런이 OPEN 으로 재개되고 에이전트가 해결·push →
      다음 폴에서 머지.

- [ ] **conflict 뒤 트리가 깨끗하다** ⭐
      위 conflict 직후 `git -C <워크트리> status` 에 머지 진행 흔적이 없다
      (`merge --abort` 됨). 안 그러면 #736 리퍼가 그 트리를 영원히 거부한다.

- [ ] **머신이 안 닿으면 실패로 남는다, 서버로 넘어가지 않는다** ⭐
      호스트 워커를 내린 뒤 stale PR 을 폴하게 한다 → `freshen_in_place_unreachable`
      + 행이 `freshen_failed` 로 백오프. **`merge_watch_reclone` 이 없어야 한다**
      (있으면 프라이버시 계약이 깨진 것).

- [ ] **server_sandbox 제품은 그대로다** (회귀)
      서버 실행 제품의 stale PR 이 예전과 똑같이 서버 클론에서 최신화된다.

## 확인 명령

```bash
docker --context colima logs -f bsvibe-prod-worker-1 | \
  grep -E "freshen_in_place_|merge_watch_(freshen|reclone|conflict)"

docker --context colima exec bsvibe-prod-postgres-1 psql -U bsvibe -d bsvibe -A -t -c \
  "select status, last_error, attempts from github_merge_watch order by created_at desc limit 5;"
```
