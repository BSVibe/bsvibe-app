# E2E — R9 canon proposals in the Brief's Needs you

Founder decision: knowledge/canon proposals arise WHILE doing work (and the
report already surfaces added knowledge), so they belong in the Brief's "Needs
you" — judged inline like every other decision — not a separate Decisions tab.

## Automated (vitest)
- [x] brief.ts inlineNeedsYou now includes the "knowledge" kind (no longer filtered out)
- [x] NeedsYou renders a "knowledge" item as a ProposalCard
- [x] ProposalCard: readable title (de-slugged action_kind) + kind chip + Accept / Reject
- [x] accepting calls acceptProposal(action_path) and re-reads the Brief (onResolved)
- [x] full PWA suite green (633), biome + tsc clean, next build ok

## Visual (Playwright preview, real globals.css)
- [x] proposal renders as a need-card consistent with the other Needs-you cards

## Prod dogfood (manual)
- [ ] A pending canon proposal shows in the Brief's "Needs you" as a card
- [ ] Accept applies it (merge/promote); Reject resolves without applying
- [ ] The "Needs you N" filter count includes proposals
