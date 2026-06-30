# E2E Checklist — J2 inline Direct-answer executor parity

Verified live on prod after deploy (record → batch fix → retest methodology).

- [ ] `POST /api/v1/messages/ask` with a question on an executor-only workspace
      returns `200` (never 500 / "Network hiccup").
- [ ] On an executor-only workspace, an inline question now produces an inline
      answer (`answered:true`) instead of always degrading — the executor serves
      it identically to a LiteLLM account.
- [ ] A slow/busy executor degrades to `answered:false` within the inline
      timeout (~45s) rather than blocking the request for the full frame timeout.
- [ ] A native (LiteLLM/ollama) chat account still answers inline (unchanged).
- [ ] K3 RAG grounding / Lift4 concept fold is reflected in the inline answer
      (imported workspace knowledge surfaces in the response).
