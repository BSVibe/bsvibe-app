"""Probe schema fix: source_truncated field and not_seen status.

Changes in outcome_demonstration.Probe:
- source_truncated: bool = False — marks probes planned from truncated source
- judge_probe returns "not_seen" instead of "contradicted" when source_truncated=True
"""
