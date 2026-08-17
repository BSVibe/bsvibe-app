"""
tests/execution/test_verification_service.py 에 추가된 두 핵심 테스트 함수.
cannot_determine 배선과 TRUNCATED 자동 승격을 검증한다.
"""

# test_verify_judge_cannot_determine_does_not_fail_run (line 1416):
#
# async def test_verify_judge_cannot_determine_does_not_fail_run() -> None:
#     contract = VerificationContract(checks=(
#         VerificationCheck(kind="command", command="true"),
#         VerificationCheck(kind="judge", criteria=("function must accept *args",)),
#     ))
#     llm = StubLlm([LoopTurn(content='{"cannot_determine": true, "reasoning": "not in view"}')])
#     svc = VerificationService(session=session, llm=llm)
#     vr = await svc.verify(run=run, ...)
#     assert vr.outcome is VerificationOutcome.PASSED       # ← 판사 abstain → PASS
#     assert vr.result["judge"]["cannot_determine"] is True  # ← 정직하게 기록

# test_verify_judge_false_with_truncated_context_does_not_fail_run (line 1447):
#
# async def test_verify_judge_false_with_truncated_context_does_not_fail_run() -> None:
#     big_content = b"def placeholder(): pass\n" + b"x" * _JUDGE_FILE_CONTEXT_BYTES
#     box = FakeBox(files={"src.py": big_content})
#     llm = StubLlm([
#         LoopTurn(content='{"setup": [], "probes": []}'),        # 데모 플래너
#         LoopTurn(content='{"passed": false, "reasoning": "function not visible"}'),
#     ])
#     vr = await svc.verify(run=run, ..., written_paths=["src.py"])
#     assert vr.outcome is VerificationOutcome.PASSED        # ← TRUNCATED → cannot_determine → PASS
#     assert vr.result["judge"]["cannot_determine"] is True
#     assert vr.result["judge"]["context_truncated"] is True
