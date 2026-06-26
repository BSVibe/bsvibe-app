# E2E — R12 knowledge note viewer (deep-link from the report)

Founder #2: the 추가한 지식 chip ("무슨 노트를 어떻게 추가했는지 모르겠어") + the note
isn't visible in the hub-capped graph. Make the chip open the ACTUAL note.

## Backend (pytest)
- [x] GET /api/v1/inside/note?path= returns a note's title + body (frontmatter stripped)
- [x] 404 for a missing file / traversal / non-note-dir path / .bsage internals
- [x] workspace-scoped — another workspace's note is not addressable (404)
- [x] report `written` carries {title, path} (vault-relative, stripped from the ABSOLUTE node_ref)
- [x] M1 route guard + module layout updated (inside.note); D35 LOC held
- [x] ruff + mypy + pytest green

## Frontend (vitest)
- [x] 추가한 지식 chips are buttons; clicking opens the note viewer with the note content
- [x] a "Related note —" reference chip is also clickable → opens the viewer
- [x] a non-note reference (a decision) stays plain text (not a button)
- [x] biome + tsc + vitest 635 + build; impeccable 0 anti-patterns

## Visual (Playwright preview, real globals.css)
- [x] centered <dialog> modal: note title + Close + rendered markdown body

## Prod dogfood (manual)
- [ ] Open a report → click an added/referenced note chip → the note opens with its content
- [ ] Esc / Close dismisses; a bad path shows a calm "couldn't open" line
