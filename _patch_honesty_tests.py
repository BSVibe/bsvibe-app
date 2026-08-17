"""Test: test_verify_judge_false_with_truncated_context_does_not_fail_run.

Verifies that a judge reply of {"passed": false} combined with a
[... TRUNCATED ...] sentinel in the context is promoted to cannot_determine,
leaving vr.outcome as PASSED and recording context_truncated=True.
"""
