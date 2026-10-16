# 워크스페이스 모델 계정은 서버를 내부망으로 겨눌 수 없다

`ModelAccount.api_base` 와 `extra_params`(`litellm.acompletion` kwargs 에 마지막으로 합쳐진다)를
아무도 검사하지 않았다. 공개 가입이면 어떤 워크스페이스든 사설 주소 · 메타데이터 주소 · 호스트 서비스로
백엔드가 매 턴 요청을 보내게 만들 수 있었다.

이제:
- `LlmClient.chat` 이 매 호출 전 **합쳐진** kwargs 를 검사: 모든 엔드포인트가 http(s) 이고 해석된 주소가
  전부 공개여야 한다. `ollama/` · `ollama_chat/` · `hosted_vllm/` … 모델은 엔드포인트를 반드시 지정
  (LiteLLM 기본값이 서버 자신의 루프백이다)
- 계정 저장(REST create/patch, MCP create)도 같은 검사 → 422 / ToolError
- 실행기 계정은 예외 (서버가 아니라 형님 워커에서 돈다)
- `BSVIBE_LLM_EGRESS_ALLOWED_HOSTS` — 배포 전역 정확-호스트 예외, 기본 비어 있음

백엔드만 — 자동 배포. 마이그레이션 없음.

## 유닛 / 배선 (로컬)

- [x] 내부 엔드포인트 9종 — 요청 전에 거절 (루프백, RFC1918, 링크로컬, CGNAT, `::1`, `0.0.0.0`)
- [x] 공개 이름이 사설 주소로 하나라도 해석되면 거절, docker 서비스 이름 거절
- [x] `extra_params.base_url` · `extra_params.api_base` 로 검사를 우회할 수 없다
- [x] 엔드포인트 없는 로컬 런타임 모델 거절 · `file://` 거절 · 해석 안 되는 호스트 거절
- [x] 대조군: 공개 엔드포인트, 엔드포인트 없는 호스팅 provider, 운영자 허용 목록
- [x] REST create/patch → 422 + 저장·변경 없음, MCP create → ToolError, 실행기 예외
- [x] 전선 절단 10곳(호출 지점 · `base_url` 키 · 전 주소 검사 · 루프백 기본값 · 허용 목록 · create · patch · 실행기 예외 · REST 매핑 · MCP 매핑) 각자 빨강

## prod (배포 후)

- [x] `bsvibe_model_accounts_create` 에 `api_base=http://169.254.169.254` → 거절, 목록에 아무것도 없음 (2026-10-15 실측)
- [ ] 다른 워크스페이스의 기존 계정 중 내부를 가리키는 것은 이제 런이 `run_drive_failed`("is not a public address")로
      멈춘다. 미리 알려면 형님 DB 읽기:
      `SELECT workspace_id, provider, litellm_model, api_base FROM model_accounts WHERE provider <> 'executor' AND (api_base IS NULL AND litellm_model ~ '^(ollama|ollama_chat|lm_studio|hosted_vllm|vllm|llamafile)/' OR api_base ~* '(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|host\.docker\.internal)')`

## 닫히지 않은 것

- DNS 리바인딩: 이름을 여기서 한 번, HTTP 클라이언트가 한 번 더 해석한다. 완전한 답은 백엔드 컨테이너의 egress 방화벽.
