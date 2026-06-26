# E2E — R2b "Learned" knowledge on the report

The report's "Learned / 새로 쓴 지식" group surfaces knowledge the run NEWLY wrote:
the founder decisions it resolved + approaches it rejected (settle activities).

## Backend (automated — tests/api/test_deliverables.py)
- [x] `learned` carries decision-resolution + negative-pattern settle summaries
- [x] the verified-work settle (file-list summary) is excluded from `learned`
- [x] a clean run (no decisions/rejections) → `learned == []` (group hides)

## Follow-up (not in this lift)
- [ ] Async canonical implementation pattern as "Learned" (needs settle→garden drain + notes-by-run query)
- [ ] Reference/Learned chips deep-link to the exact knowledge note (needs source note-id threaded through retrieval)

## Prod dogfood (manual)
- [ ] Open a report for a run where you resolved a decision → it appears under "Learned".
- [ ] Open a report for a clean verified run with no decisions → no "Learned" group.
