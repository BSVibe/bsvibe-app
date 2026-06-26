# E2E — R10 separate referenced vs written knowledge

Founder: a "Related note — settle-*.md" chip reads like knowledge this run
WROTE, not referenced. Separate 참고한 지식 (consulted) from 추가한 지식 (written),
and show what was written precisely.

## Automated (pytest)
- [x] `written` carries this run's notes from settle_drains (run_id → node_ref), de-slugged to titles
- [x] a self-written note path is EXCLUDED from `references` and appears in `written`
- [x] a genuine prior reference stays in `references`
- [x] `written` empty when settle_drains has no row for the run (drain not yet run)
- [x] D35 LOC held (proof 250 / _schemas 250 / _narrative 158); ruff + mypy clean

## Frontend (vitest)
- [x] report renders "참고한 지식" + "추가한 지식" as distinct sub-groups
- [x] Knowledge section hides when both empty
- [x] full PWA suite green (633), biome + tsc + build

## Visual (Playwright preview, real globals.css)
- [x] 참고한 지식 (gray chips) above 추가한 지식 (amber chips) — clearly separated

## Prod dogfood (manual)
- [ ] Open a report → referenced notes (prior) are separate from added notes (this run)
- [ ] An added note shows its readable title, not a raw vault path
- [ ] The run's own note does NOT appear under 참고한 지식
