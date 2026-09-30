# RLS 도구 (#959)

측정 전용. 앱이 import 하지 않는다.

| 파일 | 무엇 |
|---|---|
| `blindstmt_plugin.py` | pytest 플러그인 — **GUC 가 빈 연결에서 강제 표를 언급하는 SQL 문장**을 행 유무와 무관하게 발행 지점과 함께 기록. `BLINDSTMT_ROOT=<checkout> BLINDSTMT_OUT=<file> PYTHONPATH=tools/rls-canary uv run pytest -p blindstmt_plugin ...` |
| `canary.py` | DB 만 건드리는 배경 틱(스케줄 · 인테이크 · 클레임 · 회수 · 브리프 · settle)을 돌리고 **표별 행 증가**를 출력 |
| `run_case.sh` | prod 덤프를 일회용 PG 에 복원 → (closed 면) 정책을 닫고 → `canary.py` |
| `failclosed.sql` | 명령별 정책 24개를 빈 GUC 탈출구 없이 다시 만든다(로컬 전용) |

## ③ 재시도 전 카나리 (배포 **전**)

```bash
docker exec bsvibe-prod-postgres-1 pg_dump -U bsvibe -d bsvibe -Fc > /tmp/prod.dump   # 끝나면 지워라
tools/rls-canary/run_case.sh open   /tmp/prod.dump <checkout> open
tools/rls-canary/run_case.sh closed /tmp/prod.dump <checkout> closed
rm /tmp/prod.dump
```

두 줄의 `delta` 가 **같아야** 한다. 2026-09-29 검증: 수정 전 코드는 closed 에서 틱마다
인테이크 50 · 클레임 10(사고 그대로), #1093 이후 open = closed = 변화 없음.

⚠️ 외부 부작용 워커(알림 발송 · 전달 · 릴레이 · merge-watch · 인증 프로브)는 돌리지 않는다.
스냅샷에 대기 작업이 없으면 대부분 0건을 처리한다 — 「일이 있을 때」 경로는 약하게만 덮인다.
