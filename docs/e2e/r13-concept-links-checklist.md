# E2E — R13 clickable concept references

Founder: "Function, Backend, Common, Verification은 클릭이 안되는데". Those are canon
CONCEPTS (concepts/active/<slug>.md), so the 참고한 지식 chip should open the concept.

Reference classification (by the retriever's statement prefix):
- "Related note — <path>"        → note viewer (R12)
- "Prior decision — …"           → plain text
- "Avoid (prior rejection) — …"  → plain text
- else (a canon concept display) → concept viewer (R13), id = slug(display)

## Frontend (vitest)
- [x] a concept reference renders as a button; clicking opens the concept viewer
- [x] the concept viewer shows related concepts + the observations where it shows up
- [x] a prior-decision / rejection reference stays plain text (not a button)
- [x] a "Related note —" reference still opens the note viewer (R12 unchanged)
- [x] biome + tsc + vitest 636 + build; impeccable 0 anti-patterns

## Visual (Playwright preview, real globals.css)
- [x] concept modal: name + 관련 개념 chips + 어디에 나오는지 list (reuses the note-viewer shell)

## Prod dogfood (manual)
- [ ] Open a report → click a concept chip (Function/Backend/…) → the concept opens
- [ ] Related concepts + source observations show; a bad slug shows a calm line
