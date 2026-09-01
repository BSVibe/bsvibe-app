# `GET /deliverables/{id}/artifacts/{ref:path}` ↔ `bsvibe_deliverables_artifacts`

> 2026-09-01. 파리티 갭 **마지막 하나**. `_KNOWN_GAPS` → **0**.

## 핀의 사유가 실제와 달랐다

핀은 이렇게 적혀 있었다 — *"raw-bytes viewer surface; a workspace-token tool that
**streams arbitrary bytes** out of a deliverable is a separate design question."*

코드를 읽으니 그렇지 않았다:

| 핀이 말한 것 | 코드가 하는 것 |
|---|---|
| arbitrary bytes | **ref 화이트리스트** — 그 산출물이 선언한 `payload.artifact_refs` 에 없으면 404 |
| streams raw | **UTF-8 텍스트**를 JSON 필드에, **256 KiB 캡** |
| bytes | 바이너리는 `"Binary file, N bytes — not shown."` — **바이트를 안 준다** |
| — | traversal 가드 + 워크스페이스 스코프, 전부 404(존재 누출 없음) |

비교 대상 `bsvibe_work_file_read` 는 **런 워크트리의 임의 경로**를 읽는다. 이 표면이
오히려 **더 좁다**.

⇒ 규율 46/47 과 같은 모양: **핀의 사유도 재측정 대상이다.**

## 형님 판단 (2026-09-01)

실제로 바뀌는 축은 하나 — 파일 내용 읽기가 **런 스코프 토큰 전용**에서
**워크스페이스 토큰으로도** 가능해진다. 그래서 판단을 올렸고:
⇒ **"달자 — 같은 제약 그대로."**

## 새 형태

| | |
|---|---|
| `workflow/application/deliverable_artifact.py` | **`read_deliverable_artifact`** — 제약이 규칙에 딸려간다 |
| | **`run_artifact_store()`** — "런 파일이 어디 사는가"의 **단일 정의** |
| `api/v1/deliverables/proof.py` | 249 → **107줄** 얇은 어댑터 |
| `api/deps.py::get_artifact_store` | 위 헬퍼에 위임 (루트 정의 복사 안 함) |

⚠️ 제약이 **어댑터가 아니라 규칙의 성질**이라 MCP 툴이 브라우저보다 느슨해질 수
없다. 모든 거절은 `None` 하나로 모인다 — 잘못된 워크스페이스 · 미선언 ref ·
traversal · 파일 소실이 **구분되지 않는다**. 그게 의도다.

`lint-imports` **5 kept · 0 broken — 새 예외 0건.**

## 가드

| 테스트 | 명제 |
|---|---|
| `…serves_a_declared_ref` | 선언된 ref 는 읽힌다 |
| `…refuses_a_ref_the_deliverable_never_declared` | **화이트리스트가 규칙에 딸려간다** |
| `…reports_binary_without_the_bytes` | 바이너리는 노트만, NUL 없음 |

⚠️ 두 번째가 핵심이다. 파일은 **디스크에 실재하고 읽을 수 있다**(`.env` 를 실제로
만들어 둔다). 거절되는 **유일한 이유가 미선언**이다 — 화이트리스트가 없으면
워크스페이스 토큰이 런 디렉터리의 아무 경로나 읽는다. "없는 파일이라 404" 로
통과하는 가짜 초록이 아니다.

## 체크리스트

- [ ] `uv run pytest --cov=backend --cov=plugin --cov-fail-under=80` → green
- [ ] `uv run lint-imports` → **5 kept, 0 broken**
- [ ] `uv run mypy backend/` → clean
- [ ] `_KNOWN_GAPS` → **비어 있다** (흔적 아님 — 빈 것이 주장하는 결과다)
- [ ] 배포 후 prod: 툴 등록 + 선언 ref 읽힘 + **미선언 ref 거절**
