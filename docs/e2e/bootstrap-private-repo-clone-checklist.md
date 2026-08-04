# E2E — 제품 bootstrap이 private repo를 clone (#678)

대상: `run_product_bootstrap_job`의 clone 자격 배선.
전제: 워크스페이스에 active github 커넥터가 있고, 그 토큰이 대상 private repo에 접근 가능.

## 라이브 검증

- [ ] **private repo bootstrap 성공**
      private repo(`blas1n/BStockReport`)를 제품으로 등록 → `bsvibe_products_bootstrap_retry`
      → `bsvibe_products_show`가 `failed:clone`이 아니라 진행/`complete`로 간다.
      (수정 전 재현: 즉시 `failed:clone` + `Cloning into '...'`)

- [ ] **public repo 회귀 없음**
      github 커넥터가 있든 없든 public repo bootstrap이 종전대로 성공한다.
      (토큰이 있으면 인증 clone, 없으면 anonymous clone 둘 다 OK)

- [ ] **커넥터 없는 워크스페이스에서 private repo 실패 메시지가 원인을 말한다**
      `bootstrap_error`에 "no active github connector … connect github in Settings" 힌트가 붙는다.
      (수정 전: git stderr만 잘려 나와 원인 구분 불가)

- [ ] **자격 조회 실패가 bootstrap을 죽이지 않는다**
      KMS 키 부재/OAuth 만료 상황에서도 anonymous로 폴백해 public repo는 계속 성공하고,
      `bootstrap_clone_credential_unavailable` 경고가 로그에 남는다.

- [ ] **토큰이 로그·상태에 노출되지 않는다**
      성공/실패 어느 경로에서도 `bootstrap_error`와 구조화 로그에 토큰 문자열이 없다.
      (`redact_url_password`가 URL 경로를 덮고, 힌트 문구는 토큰을 담지 않는다)

## 유닛 커버 (자동)

`tests/products/test_bootstrap_clone_credentials.py` — 6 케이스:
active github 커넥터 → 토큰 전달 / 커넥터 없음 → `token=None` / 비활성 커넥터 무시 /
github 아닌 커넥터 무시 / 자격 없는 실패에 힌트 부착 / 자격 있는 실패엔 힌트 미부착.
