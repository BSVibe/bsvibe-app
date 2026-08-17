"""Judge-visibility fix: cannot_determine wiring and TRUNCATED promotion.

Changes in verification_service._run_judge:
- gating path: judge_pass = (True if judge_blob.get("cannot_determine") else ...)
- retrieved path: same cannot_determine → True wiring
- TRUNCATED promotion: "[... TRUNCATED" in work_block + passed=false → cannot_determine
"""
