# E2E — PWA onboarding + honest worker status

Closes the audit's new-founder-can't-reach-first-value blocker. Philosophy: NOT
a managed worker — GUIDE the user to connect their OWN self-hosted worker (like a
GitHub Actions runner) and show honest status while none is connected.

## Behavior (unit-verified — Vitest/RTL)
- [x] `getBrief()` folds `listWorkers()` → `hasLiveWorker` (`heartbeat_fresh`) + `hasProducts` into `BriefView`; degrades to `[]`/false on a workers blip (never blanks the Brief).
- [x] Brief shows the 3-step OnboardingChecklist on a first-run workspace (0 products, 0 runs); hides once the workspace has products + a live worker.
- [x] Checklist marks a step done from the live signal (worker step ✓ when `hasLiveWorker`).
- [x] WorkingNow: an active run with NO live worker shows a calm "Waiting for a worker" pill + hint instead of the ever-climbing "Working" timer.
- [x] Request FAB: a 400 (zero-product workspace) shows the localized "Create a product first…" hint, not the generic send error (and NOT the raw English backend detail — keeps English off a KO surface).
- [x] Full PWA suite (710) green; tsc --noEmit clean; biome clean; ko/en at key parity.

## Live E2E (staging/prod PWA, a fresh workspace)
- [ ] Sign in to a workspace with 0 products + 0 workers → Brief shows the onboarding checklist (create product → connect a worker → send request), not a blank "All caught up".
- [ ] The "Set up a worker" link deep-links to Settings → Models → Executor workers (the register/service install surface).
- [ ] Create a product → the "create your first product" step checks off (✓).
- [ ] With still no worker, submit a request via the + FAB → shows the "Create a product first…" hint IF no product; with a product but no worker, the run appears under Working as "Waiting for a worker" (not a climbing "Working").
- [ ] Register + `bsvibe-worker service install` a worker → `hasLiveWorker` flips true → the worker step checks off, the checklist disappears once products+worker exist, and the run flips from "Waiting for a worker" to the normal working state and completes.
- [ ] Verify the KO locale shows the localized strings (해요체) for all new copy.
