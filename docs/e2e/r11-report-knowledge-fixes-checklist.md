# E2E — R11 report button parity + knowledge path-norm

Live feedback on a real report (run b2db8986). Findings:
- #1 (clamp under 참고한 지식): R10 is CORRECT — clamp is a PRIOR run's note this run
  referenced; this run wrote only the mean note. Founder agreed (the verb-style
  note name + the note not showing in the capped hub-graph made it look like a bug).
- #2 (mean entry unclear): the written note's filename IS the slugified full
  request, so the de-slugged title is long/redundant; and a fresh low-degree note
  is capped out of the hub-graph view → a note VIEWER/deep-link is the real fix
  (follow-up, needs a note-content endpoint + a surface).
- #3 (buttons): the report footer said "승인하고 출시 / 거절" but the Brief says
  "승인 / 거절" — must be identical.

## This lift
- [x] #3: report footer reuses the SAME (decisions) namespace labels as the Brief
      DeliveryRow → Approve / Decline (승인 / 거절), identical everywhere
- [x] path-norm: a settle_drains node_ref is ABSOLUTE in prod, a reference path is
      RELATIVE — match by filename so a self-write IS excluded from referenced
- [x] backend pytest 40 + ruff + mypy; D35 LOC held (_narrative 167)
- [x] frontend biome + tsc + vitest 633 + build

## Follow-up (not here)
- [ ] #2 note viewer / deep-link: make a 추가한 지식 chip open the actual note
      (the hub-capped graph won't surface a fresh low-degree note)
