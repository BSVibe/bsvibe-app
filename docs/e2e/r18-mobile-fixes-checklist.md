# E2E — R18 mobile product access + Brief horizontal scroll

Founder (live mobile review): (1) no way to see the product rail on mobile;
(2) an unnecessary horizontal scroll on the Brief.

## #1 — mobile product access
The desktop left rail (with its PRODUCTS section) is hidden on mobile; products
were only reachable via a buried embed at the bottom of the Brief. Now:
- [x] a "제품" tab in the mobile bottom nav (4th tab, mobile-only — not in
      PRIMARY_NAV, so the desktop rail is unchanged) → /products
- [x] new /products page reuses the rail's product list (load + list + New product)
- [x] the buried brief__mobile-products embed is removed
- [x] app-shell test asserts the Product tab links to /products

## #2 — Brief horizontal scroll
- [x] `.brief { overflow-x: clip }` on mobile — the Brief is a vertical document,
      so it must never scroll horizontally (clip, not hidden → no scroll-container
      / sticky breakage; scoped to .brief so report/run code blocks are untouched)
- [x] `.need-card__options { min-width: 0 }` — reset the <fieldset> min-content
      default that can refuse to shrink on a narrow viewport

biome + tsc + vitest 635 + build; impeccable detect 0. Verified the 4-tab nav +
products page at 360px via a Playwright preview.

## Prod dogfood (manual)
- [ ] On a phone: the bottom nav shows 제품 → tap → product list → tap a product
- [ ] The Brief no longer scrolls horizontally
