# E2E — R17 product detail page redesign

Founder feedback on the mockup: remove the trust/health concept; don't show a
repo URL (internal/optional); notes are GLOBAL not per-product (no note counts);
the file tree is NOT "knowledge" and the knowledge graph isn't per-product.

Result: minimal header (name + status only) + 3 tabs — 활동 (runs + shipped),
파일 (codebase browser), 설정 (resources + connectors + delete). The old 9-panel
linear stack is gone; TrustPanel deleted.

## Frontend (vitest)
- [x] header renders name + status, NO repo URL
- [x] Activity is the default tab (recent runs + shipped)
- [x] tabs switch (Activity ↔ Files ↔ Settings)
- [x] not-found / error / empty / loading states intact; back-to-Brief intact
- [x] TrustPanel removed (component + test deleted)
- [x] biome + tsc + vitest 634 + build; impeccable detect 0 anti-patterns

## Visual (Playwright preview, real globals.css)
- [x] minimal header, pill tabs, calm runs + shipped under Activity

## Prod dogfood (manual)
- [ ] Open a product → no repo/trust/notes in the header; tabs work
- [ ] 활동: runs + shipped; 파일: file browser; 설정: resources + connectors + delete
