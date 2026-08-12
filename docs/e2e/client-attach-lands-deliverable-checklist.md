# E2E — client_attach 런이 끝나면 형님에게 도달한다

대상: `land_verified_artifacts` (공유) + `land_client_attach_deliverable` + `settle_client_attach` 배선.
전제: client_attach 제품(BStockReport-client)에 live 워커, 워크스페이스에 텔레그램 커넥터
(PR 항목까지 보려면 active github 커넥터).

배경: 서버 sandbox 런은 `finish_verified` 가 Deliverable + DeliveryEventRow + settle 활동을
만든다. 그 함수의 docstring 이 스스로 **"이 계약은 compute backend 무관"**이라고 선언한다.
그런데 client_attach 는 그 헬퍼를 안 탔다 — 런이 끝나고, 커밋하고(#735), push 하고, 그리고
**서버에 그걸 가리키는 게 아무것도 없었다**: 딜리버러블도, 승인 항목도, 알림도. 형님은 자기
손으로 `git log` 를 쳐야 작업이 끝난 걸 알았다. 근거였던 *"서버에 소스가 없으니 전달할 것이
없다"*는 #735(브랜치 push)와 #738(그 브랜치로 PR)로 **두 번 뒤집혔다**.

라이브 재현(수정 전): run `7f890b10` — `client_attach_work_pushed` → 워크트리 회수까지 전부
정상인데 승인도 텔레그램도 PR 도 없었다.

## 라이브 검증

- [ ] **런이 끝나면 승인 항목이 올라온다** ⭐
      MCP `bsvibe_direct(product_slug_or_id="bstockreport", text=<의도만>)` 로 투입 →
      런 종료 후 `bsvibe_deliverables_list(run_id=…)` 에 CODE 딜리버러블 1건,
      `bsvibe_safe_mode_list_pending()` 에 그 런의 항목.
      워커 로그: `client_attach_deliverable_landed run_id=… changed=N`.
      (수정 전 재현: 딜리버러블 0건, 로그에 그 줄 자체가 없음)

- [ ] **텔레그램이 폰에 도착한다** ⭐
      승인 → 텔레그램 도착. 본문 첫 줄이 **형님 의도**(에이전트 나레이션 아님),
      그 아래 바뀐 파일 목록, 그리고 게이트가 돌았으면 `검증: N개 확인 통과.`

- [ ] **그 승인이 PR 도 연다** (#738 라이브 실증)
      github 에 `run/<run8>` → base PR. 지금까지 #738 을 라이브로 못 본 이유가 정확히
      딜리버러블이 없어서였다.

- [ ] **아무것도 안 바꾼 런은 승인 항목을 안 만든다** ⭐
      순수 질문/조사 성격의 지시를 투입 → 런은 `verified` 로 끝나지만 딜리버러블 0건.
      로그 `client_attach_no_deliverable_nothing_changed`. **실패로 기록되지 않는다.**
      (딜리버러블은 "뭔가 했다"는 주장이다 — #735 의 빈 커밋 규칙과 같은 규칙)

- [ ] **게이트가 없는 런도 도달은 한다, 다만 PROVED 는 아니다** ⭐ (정직성 래칫)
      매니페스트 없는 레포에서 파일을 바꾼 런 → 딜리버러블은 온다,
      `work_steps.proof_state` 는 `UNTESTED` 로 남고 요약에 검증 문장이 **없다**.
      ```sql
      select ws.proof_state, d.payload->>'summary'
        from work_steps ws join deliverables d on d.run_id = ws.run_id
       where ws.run_id = '<run_id>';
      ```

- [ ] **없는 툴은 통과로 세지 않는다**
      게이트 명령 하나가 그 머신에 없어(exit 127 → `unavailable`) 기록된 런의 요약이
      그 명령을 "통과"에 포함하지 않는다. `verification_results.result.derived_gate.commands`
      의 `status=passed` 개수와 요약의 숫자가 일치.

- [ ] **서버는 여전히 소스를 갖지 않는다** ⭐
      `docker --context colima exec bsvibe-prod-backend-1 ls /app/var/runs/<run_id>` 가
      비어 있거나 없다. 딜리버러블 payload 에 `diff` 키가 없다(서버측 워크트리가 없으니
      캡처할 diff 도 없다 — 파일 목록은 **파운더 머신 git** 이 답한 것).

- [ ] **Brief 에 반영된다**
      settle 활동이 생겨(`activity_type='settle'`, `verified: true`) PWA Brief 에 뜬다.

## 확인 명령

```bash
# 워커 로그
docker --context colima logs -f bsvibe-prod-worker-1 | \
  grep -E "client_attach_|inplace_gate_|verified_deliverable_written"

# 딜리버러블 + settle
docker --context colima exec bsvibe-prod-postgres-1 psql -U bsvibe -d bsvibe -A -t -c \
  "select deliverable_type, payload->>'artifact_refs' from deliverables where run_id='<run_id>';"
```
