# E2E — client_attach 런의 push 된 브랜치가 PR 이 된다

대상: `resolve_github_binding` 의 client_attach 스킵 제거 + `_deliver_client_attach_pr`.
전제: client_attach 제품(BStockReport-client)에 live 워커, 워크스페이스에 active github 커넥터.

배경: #723 이 client_attach 에 github **바인딩 자체**를 껐다. 크래시(`git add -A` 를 없는
체크아웃에 대고 실행)는 실제였고 결론이 너무 넓었다 — 바인딩이 없으면 그 실행 모델의 런은
**영원히 PR 을 못 받는다**. 막아야 하는 것은 **서버가 소스를 갖는 것**이고, 그건 clone 을 하는
두 지점(run-setup provisioner, merge-watch freshness)이 스스로 거부한다. PR 을 여는 데엔
체크아웃이 필요 없고, #735 이후 브랜치는 이미 push 돼 있다.

## 라이브 검증

- [ ] **딜리버러블 승인 → PR 이 열린다** ⭐
      client_attach 런 투입 → 딜리버러블 승인 → github 에 `run/<run8>` 에서 base 로 가는 PR.
      (수정 전 재현: 아무 일도 안 일어남 — 로그에 `github_binding_skipped_client_attach`)

- [ ] **PR 링크가 딜리버러블에 붙는다**
      PWA 딜리버러블 카드의 diff 링크가 그 PR 을 가리킨다(`Deliverable.diff_url`).

- [ ] **서버는 소스를 갖지 않는다** ⭐
      `docker --context colima exec bsvibe-prod-backend-1 ls /app/var/runs/<run_id>` 가
      비어 있거나 없다. 로그에 `github_workspace_skipped_client_attach`.
      워커 호스트에도 그 제품의 서버측 클론이 생기지 않는다.

- [ ] **아무것도 안 만든 런은 PR 을 안 연다**
      변경 없이 끝난 client_attach 런의 딜리버러블 승인 → `skipped: no_changes`,
      PR 없음, **실패로 기록되지 않음**.

- [ ] **push 실패한 런도 PR 을 안 연다**
      네트워크를 끊고 런을 끝낸 뒤(로그 `client_attach_push_failed`) 승인 →
      브랜치가 원격에 없으므로 `no_changes`. 로컬 기록이 아니라 **github 에 물어서** 판정한다.

- [ ] **PR 본문이 원 이슈를 닫는다**
      github 이슈에서 출발한 client_attach 런의 PR 에 `Closes #N` + 이슈에 PR 링크 코멘트.

- [ ] **자동머지가 붙는다**
      `github_merge_watch` 에 행이 생기고 CI green 후 머지된다(순수 API — 체크아웃 불필요).

- [ ] **stale PR 은 서버가 최신화하지 않는다** ⭐
      base 를 앞서 보낸 뒤(PR 이 behind) → 워커 로그에
      `merge_watch_freshness_skipped_client_attach`, watch 행은 종료되고
      **서버에 재-clone 이 일어나지 않는다**. PR 은 열린 채 사람을 기다린다.

- [ ] **server_sandbox 회귀 없음**
      기존 제품의 런이 종전대로 commit→push→PR 를 서버 체크아웃에서 수행한다.

## 유닛 커버 (자동)

- `tests/delivery/test_client_attach_github_pr.py` — push 된 브랜치로 PR / head 가
  `run/<run8>`(서버 브랜치 아님) / 브랜치 없음·ahead 0 이면 no-op 성공 / 서버 자격으로 PR.
  **git ops 더블은 호출되면 raise 한다** — 기록이 아니라 덫.
- `tests/delivery/test_client_attach_source_stays_put.py` — provisioner 가 clone 거부,
  freshness resolver 가 `None`. 음성 대조 2건 확인(가드를 빼면 각각 실패).
- `plugin/github/tests/test_client.py::TestCompareBranch` — ahead_by / 404=없음 /
  ahead 0 / **403 은 계속 raise**(속도제한을 "변경 없음"으로 읽으면 전 런이 조용히 사라진다).
- `tests/workflow/test_product_aware_github_binding.py` — client_attach 가 이제 바인딩을 얻는다.

## 알려진 사정거리 밖 (후속)

stale 해진 client_attach PR 의 **최신화·conflict 해결**. 체크아웃이 파운더 머신에 있으므로
거기서 해야 하고(#734 의 워크트리 + #702 의 exec 채널이 재료다), 서버에서 하면 프라이버시
계약이 깨진다. 지금은 정직하게 멈춘다.
