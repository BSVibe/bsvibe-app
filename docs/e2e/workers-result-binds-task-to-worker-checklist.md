# E2E — /workers/result 는 태스크를 인증된 워커에 바인딩한다 (H1)

`POST /api/v1/workers/result` 가 body 의 `task_id` 를 `_ = worker  # auth only` 로
소유 검사 없이 닫았다. 아무 워커 토큰으로 남의 태스크를 임의 output 으로 종료 →
상대 워크스페이스 에이전트 루프가 그 output 을 소비(교차 테넌트 주입 + 런 DoS).

## 검증

- [x] RED: 수정 전 `test_record_result_rejects_a_foreign_worker` 실패
- [x] `record_result` 가 `task.worker_id != worker_id` 거부(데이터 계층 바인딩)
- [x] terminal 태스크 재종료/output 덮어쓰기 거부(`status != "dispatched"`)
- [x] 거부는 `None` 반환 + route 200 유지 → 공격자 오라클 없음
- [x] HTTP 표면 실증: 다른 유효 워커 토큰이 남의 태스크 못 닫음(`test_result_from_a_foreign_worker_is_refused`)
- [x] 정당 경로 무손상: client_attach exec 은 `dispatch_task(worker_id=...)` 로 태스크에 worker_id 세팅 → 통과
- [x] dispatch/adapter/client_worker_manager 전 테스트 통과, 미사용 artifact_store dead param 제거
- [ ] 배포 후 prod: 정상 워커 런이 계속 완료되는지 관측(회귀 없음) — deliverables 생성 지속 확인

## 배포 주의

`record_result` 시그니처 변경(worker_id 필수). 프로덕션 호출자는 `/result` 라우트
하나뿐이며 `worker.id` 를 넘기도록 배선됨. 배포 시 워커↔백엔드 버전 스큐가 잠깐
생겨도 워커는 HTTP body 만 보내므로 무관(가드는 서버 측).
