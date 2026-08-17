"""
outcome_demonstration.py 의 source_truncated + not_seen + summarize 동작을
검증하는 테스트 스니펫 (tests/execution/test_outcome_demonstration.py).
"""

# ProbeStatus 에 "not_seen" 가 추가됨:
# ProbeStatus = Literal["matched", "contradicted", "unavailable", "not_seen"]

# judge_probe: source_truncated=True 이면 불일치 → not_seen (contradicted 아님)
# def test_judge_probe_source_truncated_mismatch_is_not_seen():
#     p = Probe(name="f", command="echo hi", source_truncated=True,
#               expect_stdout_contains=("hi",), expect_exit_zero=True)
#     o = Observation(exit_code=1, stdout="nope", stderr="")
#     assert judge_probe(p, o) == "not_seen"

# summarize: not_seen 상태는 failed 가 아니라 undemonstrable
# def test_not_seen_does_not_fail_summarize():
#     p = Probe(name="f", command="echo hi", source_truncated=True,
#               expect_stdout_contains=("hi",))
#     o = Observation(exit_code=1, stdout="", stderr="")
#     r = ProbeResult(probe=p, observation=o, status="not_seen")
#     assert summarize([r]) == "undemonstrable"  # ← 실패가 아닌 하강

# summarize: contradicted 는 여전히 failed (회귀 방지)
# def test_contradicted_still_fails():
#     p = Probe(name="f", command="echo hi", source_truncated=False,
#               expect_stdout_contains=("hi",))
#     o = Observation(exit_code=1, stdout="nope", stderr="")
#     r = ProbeResult(probe=p, observation=o, status="contradicted")
#     assert summarize([r]) == "failed"
