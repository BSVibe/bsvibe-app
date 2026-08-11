# E2E — 제품이 자기 검증 시크릿을 선언하고, 그게 체크에 닿는다

대상: `verify_secrets`(도메인) + `product_secrets`(쓰기 봉인) + dispatch `exec_env` 채널 +
워커 env 적용 + 스택 `-e NAME`.

배경: 브라우저 프로브가 어떤 계정으로 로그인할지, HTTP 프로브가 어떤 키를 낼지는 **제품마다
다른 사실**이다. 플랫폼이 정할 일이 아니라 **안전하게 담아 체크까지 나르는 배관**만 주면 된다.
테스트 툴체인도 같다 — 제품이 `verify_stack.image` 로 지목하면 되고(#737), BSVibe 에
Playwright 를 깔 이유가 없다.

⚠️ 핵심 제약: exec 명령 문자열은 `executor_tasks.prompt` 에 **그대로 저장**되고 트림 없는 Redis
스트림에도 실린다. 그래서 값은 **명령 안이 아니라 옆으로** 간다(`mcp` 토큰과 같은 선례).

## 라이브 검증

- [ ] **평문으로 쓰면 암호화돼 저장된다** ⭐
      MCP `bsvibe_products_set_metadata` 로 `{"verify_secrets":{"BSVIBE_TEST_PASSWORD":"<실값>"}}`
      → DB 에서 `select product_metadata from products where id=…` 이 `enc:` 로 시작하는
      ciphertext 여야 한다. 평문이 보이면 즉시 중단.

- [ ] **읽으면 가려진다**
      MCP `bsvibe_products_show` 와 REST `GET /api/v1/products/{id}` 둘 다
      `verify_secrets: {"BSVIBE_TEST_PASSWORD":"***"}`. ciphertext 도 나가면 안 된다.

- [ ] **왕복이 시크릿을 지우지 않는다** ⭐
      PWA 제품 설정에서 **이름만** 바꾸고 저장 → 시크릿이 그대로 남아 있다(다시 읽어 `***`).
      (수정 전 재현 없음 — 이 결함은 마스크를 그대로 받아쓰면 생긴다)

- [ ] **컨테이너 안에서 값이 보인다**
      그 제품의 런에서 체크가 `sh -lc 'test -n "$BSVIBE_TEST_PASSWORD"'` 를 돌면 exit 0.

- [ ] **명령·DB·로그 어디에도 값이 없다** ⭐
      `select prompt from executor_tasks where run_id='…'` 에 값 없음(이름만).
      `docker --context colima logs bsvibe-prod-worker-1 | grep <값>` 도 0건.
      워커 로그엔 `exec_task_received env_names=[BSVIBE_TEST_PASSWORD]` 만.

- [ ] **키가 없어도 시크릿 없는 제품은 멀쩡하다**
      `verify_secrets` 를 안 쓰는 제품의 metadata 수정이 KMS 키와 무관하게 성공한다.

- [ ] **복호 실패는 런을 안 죽인다**
      키를 회전시킨 뒤 그 제품 런 → 런은 돌고 체크가 자기 방식으로 실패한다
      (로그 `verify_secrets_cipher_unavailable` 또는 값 없음). 500 이나 크래시가 아니다.

## 유닛 커버 (자동)

- `tests/workflow/domain/test_verify_secrets.py` — 봉인/이중봉인 방지/마스크=보존/빈값=삭제/
  이름 검증(`-e NAME` 이 되므로 공백·`=` 금지)/가림/복호 실패는 drop.
  **실제 cipher 를 씀** — 평문을 품는 가짜 encrypt 는 자기가 답을 넣어주는 테스트다.
- `tests/workflow/test_verify_secret_transport.py` — 명령엔 이름만 / DB 행엔 값 없음 /
  Redis 페이로드엔 값 있음 / 로그엔 값 없음 / **boot exec 에만** env 전달 /
  시크릿 없는 제품은 `env` 인자 자체가 안 붙음.
- `tests/api/test_product_secret_seams.py` — REST 응답 모델이 마스킹, 왕복이 안 지움.
- 음성 대조 확인: 값을 `-e NAME=value` 로 인라인하면 명령 단언이 실패.

## 알려진 사정거리 밖 (정직한 한계)

- **Redis 스트림**이 노출 경계다. 값은 DB 에도 로그에도 안 남지만 dispatch 페이로드에는
  실리고, 그 스트림은 트림되지 않는다. per-run MCP 토큰이 이미 같은 경로를 쓴다.
  더 좁히려면 워커가 자기 토큰으로 백엔드에서 당겨가는 별도 경로가 필요하다.
- 시크릿은 **컨테이너 안 프로세스 환경**에 있다. 그 안에서 도는 체크는 전부 읽을 수 있다 —
  제품이 자기 체크를 신뢰한다는 전제이고, 그게 `verify_stack: null` 과 같은 성격의 선언이다.
