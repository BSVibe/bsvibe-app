# E2E — L10 Direct questions answered inline (no executor crash) (#4 + #5)

Founder feedback: a Direct *question* should be answered right in the Direct
modal; and currently a question fails with "loop crashed: executor chat task …
failed: exit 1" because it was routed into the coding-agent executor.

Root cause (#5): a `knowledge_only` ask was handed `CALLER_AGENT_LOOP_ACT`'s LLM,
which for this workspace routes to the executor (a coding-agent CLI) — it can't
answer a chat prompt. Fix: knowledge answers use a CHAT model (`CALLER_FRAME`).

## Backend (automated — `tests/glue/test_direct_ask.py`)
- [x] `is_question` classifies questions vs work requests (deterministic)
- [x] `POST /api/v1/messages/ask` on a work request → `answered:false` (PWA dispatches)
- [x] on a question → `answered:true` + inline `answer` (chat model)
- [x] on a question with no chat model → `answered:false` (falls back to dispatch)
- [x] `agent_runtime` knowledge_only branch resolves `CALLER_FRAME` (chat), not the executor — regression: `test_knowledge_only_route` green

## PWA (automated — `apps/pwa/test/direct-compose.test.tsx`)
- [x] a question is answered INLINE in the modal and is NOT dispatched as a run
- [x] a work request (answered=false) is dispatched via `POST /api/v1/messages`

## Prod dogfood (manual — verify at final review)
- [ ] Open Direct, type a question ("지금 프로젝트 상황 어때?") → an **answer appears
      inline in the modal** (no run created, no "executor chat task failed" crash).
- [ ] Type a work request ("build a small util") → dispatched as a run ("working on it").
- [ ] A question grounded in workspace knowledge reflects that knowledge in the answer.
