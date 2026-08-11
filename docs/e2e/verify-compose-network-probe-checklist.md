# E2E — compose 제품의 체크가 스택 네트워크 안에서 돈다 (전 표면 검증 5b)

대상: `derive_stack_plan` 의 compose 분기 → prober 컨테이너.
전제: compose 제품(bsvibe-app 자신)이 client_attach 로 등록돼 있고 live 워커가 붙어 있음.

배경: 격리 오버레이(#724)는 **호스트 포트를 하나도 발행하지 않는다** — 그게 일회용 스택이
prod 의 5442/6387/8700/3700 과 싸우지 않게 하는 유일한 장치다. 그 대가로 **파운더 머신에서
도는 명령은 방금 띄운 스택에 닿을 수 없다**(`localhost:8700` 은 prod 의 것이거나 아무의 것도
아니다). 그래서 체크는 스택 네트워크에 붙은 prober 안에서 돈다.

## 라이브 검증

- [ ] **prober 가 뜨고 스택 네트워크에 붙는다**
      워커 로그에 `verify_stack_ready source=compose`, 이어서
      `docker ps --filter name=verify-slot-<i>-probe` 가 러닝 컨테이너 1개.
      `docker inspect verify-slot-<i>-probe -f '{{json .NetworkSettings.Networks}}'` 에
      `verify-slot-<i>_default` 가 있다.

- [ ] **체크가 그 안에서 돈다** ⭐
      워커에 나간 명령이 `docker exec -w /work verify-slot-<i>-probe sh -lc '<체크>'` 형태.
      (수정 전 재현: compose 제품은 `wrap` 이 항등이라 체크가 **호스트에서** 돌았다)

- [ ] **서비스 이름으로 닿는다**
      prober 안에서 `wget -qO- http://backend:8000/api/health` 가 200 을 준다.
      compose 임베디드 DNS 가 `backend`/`postgres`/`redis`/`pwa` 를 해석한다.

- [ ] **호스트에서는 못 닿는다** (음성 대조)
      같은 시각 호스트에서 `curl -s localhost:8700/api/health` 는 **prod** 를 가리키거나
      실패한다 — 검증 스택이 아니다. 오버레이가 포트를 발행하지 않는 것이 정상이다.

- [ ] **소스가 prober 에 도착한다**
      `docker exec verify-slot-<i>-probe ls /work` 에 레포 내용이 있고 `.env`/`.venv` 는 없다.

- [ ] **회수: prober 가 스택보다 먼저 나간다** ⭐
      런 종료 후 `docker ps -a --filter name=verify-slot-<i>` 비어 있고,
      **`docker network ls --filter label=com.docker.compose.project=verify-slot-<i>` 도 비어 있다.**
      (실측 2026-08-11: prober 가 붙어 있으면 `compose down -v` 가 네트워크를 못 지우면서
      **exit 0** 을 낸다 — 런마다 네트워크 하나씩 누수하며 성공을 보고한다)

- [ ] **CLI 제품 회귀 없음**
      BStockReport(compose 없음) 런이 종전대로 `source=container` 로 돌고 통과한다.

- [ ] **슬롯 재획득이 죽은 점유자의 prober 도 치운다**
      prober 를 남긴 채 워커를 재시작 → 다음 런이 같은 슬롯을 잡을 때 방어적 teardown 이
      `verify-slot-<i>-probe` 를 먼저 지운다.

## 유닛 커버 (자동)

- `tests/workflow/test_verify_stack_plan.py::TestComposeProber` — 네트워크 부착 / prober 안에서
  실행 / 슬롯 이름 / **teardown 순서** / 소스 복사 / `.env` 미전파 / 툴체인 없는 compose 레포의
  정직한 한계 / 이미지 오버라이드가 스택 종류를 안 바꿈.
- `tests/workflow/test_verify_stack_compose_real_docker.py` — **실 docker + 실 compose 스택**:
  prober 는 `http://svc:8080` 에 닿고 **호스트는 그 포트에 못 닿는다**(짝을 이룬 단언 — 한쪽만으론
  무의미), 소스 도착, `.env` 미전파, 회수 후 컨테이너·**네트워크** 둘 다 0.
- 음성 대조 2건 (확인함): `--network` 를 빼면 `wget: bad address 'svc:8080'` 로 실패,
  teardown 순서를 뒤집으면 네트워크 누수 단언이 실패.

## 알려진 사정거리 밖

- prober 는 `<project>_default` 에만 붙는다. `sandboxnet`(샌드박스 컨트롤 플레인)은 의도적으로
  제외 — 데이터 서비스와 브로드캐스트 도메인을 분리해 둔 설계를 prober 가 무너뜨리면 안 된다.
- compose 레포가 **아무 툴체인도 선언하지 않으면** prober 가 없다(이미지를 지어내지 않는다).
  오늘 그런 제품은 없고, 그때는 로그에 `verify_stack_has_no_way_in` 이 남는다.
