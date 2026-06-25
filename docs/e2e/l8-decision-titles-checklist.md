# E2E — L8 Decision/review titles are plain-language (+ "View report")

Founder feedback: the review surfaces (Decisions, Brief) showed the RAW Direction
as the task title — verbose, truncated, developer-y ("In the bsvibe-app product,
add a pure utility function `mean(values: list[float]) -> float` in
backend/common/mean.py th…"). And "근거 보기 / View proof" reads better as a report.

## Backend (automated)
- [x] frame stage produces `summary_title` (short, plain) — `tests/glue/test_frame_summary_title.py`
- [x] `summary_title` is `None` when the LLM omits it / on the keyword (no-LLM) fallback
- [x] blank/odd `summary_title` is dropped; long values are length-capped
- [x] `GET /api/v1/runs` + `/{id}` expose `summary_title` + `framed_intent` (frame block)

## PWA (automated)
- [x] `buildReviewLookup` title prefers `summary_title` → `framed_intent` → deliverable summary → raw intent (`review-context.test.ts`)
- [x] Brief work-stream / active-work titles use the same preference (`brief-data.test.tsx`)
- [x] the Decisions proof link label is "View report" / "보고서 보기" (`decisions-page.test.tsx`)

## Prod dogfood (manual — verify at final review)
- [ ] Submit a new run with a developer-y Direction → the Decisions / Brief row title
      reads as a SHORT plain-language summary (e.g. "Add a mean helper"), not the raw text.
- [ ] An OLD run (framed before L8, no `summary_title`) → title falls back to the
      cleaner `framed_intent`, not the verbose raw Direction.
- [ ] The proof link on decision/resolved/delivery rows reads "보고서 보기 / View report".
- [ ] (ko workspace) a new run's `summary_title` is in Korean (follows the frame language).
