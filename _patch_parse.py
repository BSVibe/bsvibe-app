"""
verification_service.py 에 추가된 _TRUNCATION_SENTINEL 상수와
_judge_file_context 메서드의 git diff 우선 / blob 폴백 전략 스니펫.
"""

# verification_service.py 에 정의된 상수
_TRUNCATION_SENTINEL = "[... TRUNCATED"

# _judge_file_context 전략:
# 1. git diff HEAD~1 HEAD -- <path> 실행
#    → diff --git 또는 @@ 가 포함되면 유효한 diff
#    → 크기가 _JUDGE_FILE_CONTEXT_BYTES 를 초과하면 [... TRUNCATED ...] 마커 삽입
# 2. diff 실패 시 파일 blob 읽기 (fallback)
#    → 파일 크기가 한도 초과시 [... TRUNCATED ...] 마커 삽입
# 3. 파일이 6개 이상이면 처음 5개만 처리하고 [... TRUNCATED ...] 추가
# 4. 아무것도 읽을 수 없으면 "[... TRUNCATED ...] — file content unreadable" 반환

# _run_judge 에서 TRUNCATED 자동 승격:
# if _TRUNCATION_SENTINEL in work_block and not verdict.get("cannot_determine")
#        and not verdict.get("passed", True):
#     return {"cannot_determine": True, "reasoning": f"context truncated — {verdict.get('reasoning', '')}",
#             "context_truncated": True}
