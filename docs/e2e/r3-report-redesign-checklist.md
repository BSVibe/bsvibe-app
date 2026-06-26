# E2E — R3 Deliverable Report redesign

The Deliverable Report is rebuilt to a calm, editorial structure (PWA): a status
pill + plain title + meta chips, the narrative LEAD ("What this did"), an optional
quiet Note on a non-passed signal, a verification CHECKLIST ("How it was
verified"), a Knowledge group (Referenced + future-ready Learned chips), the diff
DEMOTED behind a collapsed disclosure, and a de-emphasized footer rollback.

## Frontend (automated — `apps/pwa/test/delivery-report.test.tsx`)
- [x] header status pill reads "Verified" (with a check) when `verified`, else "Needs review"
- [x] "What this did" LEADS with the `narrative`, and falls back to `request` when narrative is null
- [x] "Note" appears ONLY when the strongest verification outcome is not `passed` (carries a result message); omitted on a clean pass
- [x] "How it was verified" renders a CHECKLIST (clean label + passed tag), L12 knowledge filtered out
- [x] knowledge-only verifications read the calm "no additional checks beyond the referenced knowledge" line (not "nothing verified")
- [x] "Knowledge" renders Referenced chips; a Learned sub-group renders only when `report.learned` is non-empty; the whole section is hidden when neither exists
- [x] the captured diff sits BEHIND a collapsed `<details>` disclosure (not open on load), labelled with the file count
- [x] rollback lives in the de-emphasized `<footer>`; hidden for a pure `direct_output` answer
- [x] calm states preserved: no-verification, not-found (404), inline error, loading, truncated artifact note
- [x] sibling suites still green: diff-viewer / markdown / artifact-switch render inside the disclosure body

## Gates
- [x] `pnpm lint` (biome) clean — en/ko `report.*` parity, no dup keys
- [x] `pnpm exec tsc --noEmit` clean
- [x] `pnpm exec vitest run` — full suite green
- [x] `pnpm build` succeeds

## Prod dogfood (manual — verify at final review)
- [ ] Open a verified deliverable's report → green "Verified" pill, plain title, the narrative reads as the lead, and the diff is collapsed under "See what files changed (N)".
- [ ] Open a deliverable whose verification did not pass → the quiet amber Note surfaces the failure message; pill reads "Needs review".
- [ ] Expand the "See what files changed" disclosure → the existing file/diff viewer works unchanged (switch files, markdown render, content fallback).
- [ ] Referenced knowledge renders as chips under "Knowledge"; "Roll back" sits quiet in the footer and still reverses the external action.
