# plugin seed `source_ref` 제거 — 검증 체크리스트

> 2026-08-29. 4개 임포트 플러그인(claude · gpt · notion · obsidian)이 seed 에
> 넣던 `source_ref` 를 지운다. #846 이 커넥터 `region` 을 지운 것과 같은 모양 —
> **수신자가 버리는 값**이다.

## 왜 지웠나 — 측정

| 측정 | 결과 |
|---|---|
| `source_ref` 를 읽는 프로덕션 코드 | **0** (플러그인 자기 자신 외) |
| `write_seed` 가 `data` 에서 읽는 키 | `title` · `tags` · `content` **뿐** |
| 4개 플러그인이 title+content 를 채우나 | **예** → 본문이 `data["content"]` 로 잡혀 노트에 도달 안 함 |
| prod 에 설치된 해당 커넥터 | **0** (`github` · `telegram` 만 존재) |
| `connector_accounts.last_import_at` | **전 행 NULL** — 임포트가 한 번도 안 돌았다 |
| prod vault 의 `seeds/` 디렉터리 | **없음** (워크스페이스 3개 전부) |

양성 대조군: 같은 테이블에 `github` · `telegram` 6행이 살아 있어 쿼리가 실제로
행을 찾을 수 있음을 확인했다.

## 주석이 근거로 든 메커니즘은 이 값을 안 썼다

네 플러그인이 똑같이 주장했다 — *"re-imports … hit the IngestCompiler
content-hash dedup on the same key"*. 실제 키는

```python
cache_key = f"{rel_path}:{content_hash}"   # backend/knowledge/ingest/llm_extractor.py
```

로 **`source_ref` 를 쓰지 않는다.** 세 가지가 겹쳐 있었다:

1. 키에 `source_ref` 가 없다
2. `rel_path` 는 seed 파일마다 새 타임스탬프(`%Y-%m-%d_%H%M%S.md`)라 **양쪽 절반이
   다 달라진다**
3. `_processed_hashes` 는 `_MAX_CACHE_SIZE` 로 축출되는 **프로세스 메모리 LRU** 라
   애초에 영속 dedup 이 아니다 — 재시작하면 잊는다

## ⚠️ 남는 갭 — 재임포트는 여전히 중복을 만든다

이 PR 은 그것을 **고치지 않는다.** 지운 것은 *막고 있다는 거짓 주장*이지 중복
방지 기능이 아니었다. 인스턴스 0인 경로에 dedup 서브시스템을 짓지 않는다는
판단(형님, 2026-08-29).

커서를 저장하는 플러그인도 **0개**다 — `resolve_plugin_state_path`(영속 상태 API)
는 존재하는데 호출자가 없다. `since` 는 사용자가 주는 필터일 뿐 자동 워터마크가
아니고, notion · obsidian 은 그것도 없어 매 실행마다 전량 재수집한다.

**이 커넥터들을 실제로 쓰기 시작하기 전에 dedup 을 먼저 정해야 한다.** 그때
확인할 것: 재임포트가 seed 파일을 새로 만드는가(동작 테스트 필요 — 지금까지는
코드만 읽었다).

## 체크리스트

- [x] `source_ref` 를 쓰거나 읽는 코드가 트리 전체에 남아 있지 않다
      — `test_no_code_still_writes_or_reads_the_provenance_seed_key` (AST, dict 키 ·
      첨자 · `.get()` 세 형태)
- [x] 음성 대조군: 세 형태로 각각 재도입했을 때 가드가 **셋 다** 떨어진다
- [x] 양성 대조군: 스캐너가 살아남아야 하는 키(`content`)를 4개 플러그인에서 찾는다
      — 스캐너가 고장 나 있으면 부재 주장이 공짜로 참이 된다
- [x] 양성 대조군: 4개 플러그인이 여전히 `title` + `content` 를 채운다
      (하나라도 빠지면 본문이 전량 YAML 덤프로 바뀐다)
- [x] `write_seed` 가 읽는 키가 `title`/`tags`/`content` 그대로다 — 삭제가 안전한
      **이유** 자체를 명제로 박았다
- [x] `source_ref` 를 식별자로 빌려 쓰던 테스트 단언 10건을 **`title` 축으로 이설**
      — 커버리지를 지우지 않았다 (claude · gpt 는 픽스처 제목이 대화마다 유일)
- [x] 거짓 주장을 담은 산문 정정 — 플러그인 3종 `input_schema` 설명,
      `plugin/obsidian/client.py` docstring, `plugin/obsidian/E2E_CHECKLIST.md`
- [x] `plugin/` 스위트 642건 통과
- [x] 전체 게이트 (fresh PG · pytest+cov80 · ruff · format · mypy · lint-imports)

## 실사용 검증은 불가 — 그리고 그게 삭제 근거다

이 4개 커넥터는 prod 에 설치돼 있지 않고 임포트가 한 번도 돈 적이 없다. 브라우저
E2E 로 누를 표면 자체가 없다. **누를 표면이 없다는 사실이 이 값을 지우는 근거**라
검증 불가를 갭으로 적지 않는다.
