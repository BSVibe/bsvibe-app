# E2E — R7 Brief Needs-you cards (mockup fidelity) + remove trust arrows

The merged redesign reused the old compact decision rows, so the Brief looked
unchanged. R7 rebuilds the pending decisions as the mockup's cards and removes
the rail trust-trend arrows.

## Automated (vitest)
- [x] CheckpointRow renders options as selectable CHIPS + a persistent free-text input
- [x] a typed answer overrides a selected chip (verbatim off-list reply POSTed)
- [x] submit disabled until a chip is selected or text is typed
- [x] DeliveryRow / CheckpointRow / DeliveryRunGroupRow render as `.need-card` with amber status
- [x] Brief "Needs you" + /decisions pending both render the cards (shared)
- [x] rail renders NO trust trend glyph (removed)
- [x] full PWA suite green (633), biome clean, tsc clean, next build ok

## Visual (Playwright preview against the approved mockup)
- [x] cards: title + product chip + amber status ("Ready to ship" / "Needs your answer")
- [x] Approve & ship (dark) / Decline / View report; option chips; "Or type your own" + Answer
- [x] impeccable detect: 0 anti-patterns

## Prod dogfood (manual)
- [ ] Open the Brief → pending items render as distinct cards (not a hairline list)
- [ ] Pick an option chip OR type a custom answer → resolve works
- [ ] Approve a held delivery from its card
- [ ] The left rail shows products with no trend arrows
