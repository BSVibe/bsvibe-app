# 샌드박스 방화벽 — 사용자 정의 네트워크(`sbxnet-*`) 체크리스트

방화벽(`deploy/sandbox-dind-firewall.sh`)은 `docker0` 와 `br+`(모든 사용자 정의 브리지)에 건다.
test-DB 사이드카를 켠 상태에서 실제로 막히는지 확인한다.

## 준비

- [ ] 사이드카를 켠다(test-DB 사이드카 설정 on) 후 검증이 필요한 런을 하나 띄운다.
- [ ] DinD 에서 `docker network ls | grep sbxnet-` 로 런의 네트워크가 보인다.
- [ ] DinD 로그에 `[sbx-fw] verified: every isolation rule is present` 가 있다.
- [ ] DinD 에서 `iptables -S DOCKER-USER` 와 `iptables -S INPUT` 에 `-i br+` 규칙(사설 대역 4개 DROP, 2375 DROP)이 있다.

## 프로브 (샌드박스 컨테이너 `bsvibe-sbx-*` 안에서, sbxnet 에 붙은 상태)

- [ ] `<sbxnet 게이트웨이>:2375` (`docker network inspect` 로 게이트웨이 확인) → **BLOCKED**
- [ ] `postgres:5432`, `redis:6379`, `backend:8000` 의 사설 IP / `10.x` → **BLOCKED**
- [ ] `172.17.0.1` 및 `169.254.169.254` → **BLOCKED**
- [ ] **[양성 대조군]** 사이드카 PG(같은 sbxnet) 접속 → **OPEN**
- [ ] **[양성 대조군]** 공용 `example.com:443` → **OPEN**

## 회귀

- [ ] backend/worker → `sandbox-dind:2375` 관리 경로가 그대로 동작한다.
- [ ] 사이드카를 끄면 기존 `docker0` 프로브가 여전히 BLOCKED 이다.

## xt_physdev 미지원 호스트

- [ ] 호스트에 `xt_physdev` 가 없어도 DinD 가 정상 기동한다(사이드카 꺼짐 포함).
- [ ] DinD 로그에 `[sbx-fw] WARN: xt_physdev unavailable` 와 `verified` 가 함께 남는다.
- [ ] 이 경우에도 위 프로브의 2375 / 내부 대역 → **BLOCKED** 가 그대로 성립한다(DROP 규칙은 physdev RETURN 에 의존하지 않음).
