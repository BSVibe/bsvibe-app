"""
outcome_demonstration.py 의 Probe 데이터클래스에 추가된 source_truncated 필드와
judge_probe 함수의 not_seen 반환 로직 스니펫.
"""

# outcome_demonstration.py — Probe 데이터클래스 (관련 부분)
# @dataclass(frozen=True)
# class Probe:
#     name: str
#     command: str
#     expect_exit_zero: bool = True
#     expect_stdout_contains: tuple[str, ...] = ()
#     source_truncated: bool = False   # ← 이번에 추가된 필드
#
# def to_dict(self) -> dict[str, Any]:
#     d = {...}
#     if self.source_truncated:
#         d["source_truncated"] = True
#     return d

# outcome_demonstration.py — judge_probe (관련 부분)
# def judge_probe(probe: Probe, obs: Observation) -> ProbeStatus:
#     ...
#     exit_ok = (obs.exit_code == 0) == probe.expect_exit_zero
#     stdout_ok = all(s in combined for s in probe.expect_stdout_contains)
#     if exit_ok and stdout_ok:
#         return "matched"
#     return "not_seen" if probe.source_truncated else "contradicted"
#                          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#     플래너가 잘린 소스를 봤을 때: 불일치 → not_seen (단정적 판정 금지)

# outcome_demonstration.py — summarize (관련 부분)
# def summarize(plan_or_results, results=None, *, contradiction_fails=True):
#     ...
#     statuses = [r.status for r in actual]
#     if contradiction_fails and "contradicted" in statuses:
#         return "failed"
#     if "matched" in statuses:
#         return "demonstrated"
#     return "undemonstrable"   # ← not_seen 은 여기에 해당 (failed 아님)
