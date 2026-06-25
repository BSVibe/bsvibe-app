# E2E — L1 Direct question answered first (#12)

Founder rule: a Direct request that is a *question* is answered directly, never
routed into the agent loop where it stands down with "couldn't complete".

## Unit (automated — `tests/glue/test_frame_answer_first.py`)
- [x] Korean status question, no frame LLM → `knowledge_only`
- [x] English question, no frame LLM → `knowledge_only`
- [x] Question with a build NOUN ("api") but no build verb → `knowledge_only`
- [x] Build request phrased as a question ("can you build…?") → `agent_loop`
- [x] Non-question imperative ("create the weekly digest") → `agent_loop` (unchanged)
- [x] Question with a WORK artifact default (page) → `agent_loop` (coherence)
- [x] LLM mislabels a question as `agent_loop` → upgraded to `knowledge_only`
- [x] LLM tags a build-question with `code` → stays `agent_loop`

## Prod dogfood (manual — verify at final review)
- [ ] Send a Direct question (e.g. "지금 프로젝트 상황 어때?") with no product binding →
      run resolves via `KnowledgeAnswerOrchestrator`, returns a direct answer
      (DIRECT_OUTPUT deliverable, `verified`), NOT `failed`/`cancelled`.
- [ ] Send an explicit build ("build a small TTL cache in the backend") → still
      runs the agent loop and produces code (no regression).
