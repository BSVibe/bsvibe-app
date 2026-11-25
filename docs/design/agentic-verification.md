# Agentic verification — the run checks its own work the way a careful engineer does

> 2026-11-17. Starts from 형님's question: *"There is already a self-feedback loop — if it worked, the review could
> never find a high. The loop sounds like static checks; a real Claude Code session verifies its own work. That is
> what I wanted to deepen."*
>
> Builds on [`production-verification.md`](./production-verification.md) (2026-08-10: full-surface verification on
> a disposable stack, before merge). That design was built, but it is wired into one path only (§1.3).

## 1. What the loop does today (as built, `origin/main` 2026-11-17)

Every claim here was read from code; `VS` = `backend/workflow/application/verification_service.py`.

### 1.1 One verification round, in order

| # | Stage | Executes the product? | Reads with | Can fail the round | Reaches the agent |
|---|---|---|---|---|---|
| 0 | No-work nudge · EvidenceReminder · precheck (`_drive_loop.py:399-439`) | precheck runs declared quick commands | — | precheck only (3×/drive) | yes, as a turn |
| 1 | Merge `main` into the worktree (VS:640-702) | no | git | yes (`merge_conflict`) | yes |
| 2 | Agent's declared command checks (VS:1113) | its own tests | — | only when no derived gate ran | yes, listed last |
| 3 | Outcome demonstration (VS:1475) | **yes — the only stage meant to**: one tool-less LLM plan of one-line probes (`python -c …`) in the plain sandbox; deterministic stdout/exit judge | changed non-test files, ≤12 KB | code surface only; truncated source or no plan ⇒ `undemonstrable`, never fails | yes (`PROBE FAILED`) |
| 4 | Scope check (VS:1668) | no | intent + paths | no | only if another stage failed |
| 5 | LLM judge on agent-declared criteria (VS:1724) | no | diff of 5 paths, ≤12 KB, no tools | yes; `cannot_determine` passes | yes |
| 6 | Derived gate (VS:1321) | the repo's own lint/test/CI commands, chosen by an LLM deriver + product `floor` | manifests, CI files | yes (fail-closed when a manifest exists) | yes |
| 7 | Requirements ledger (VS:1007) | `covers`-tagged commands only | per item: cited tests, note, clipped diff, named definitions; no tools | yes (`missing`/`rejected`) | yes (`REQUIREMENTS NOT SHOWN`) |
| 7b | R4 counterfactual (`counterfactual_check.py:149`) | reverts non-test code and re-runs cited guard tests | — | **no** (report-only) | **no** |
| 8 | Product `final` commands (VS:884-890) | full CI-equivalent | — | yes | yes |
| 9 | Independent review (VS:914, after PASS) | **no** | full diff ≤30k + call-site grep; **no tools** | **no** (report-only) | **no — founder only**, via the Safe Mode card |

### 1.2 What the agent is told to do itself

The agent has `shell_exec` and is asked to declare checks first and run the repo's lint / type / test
(`agent_briefing.py:50-101`). **Nothing asks it to call a real entry point or to try to break its change.**

### 1.3 A promise the prompt makes that the server path does not keep

`agent_briefing.py:95-100` tells the agent *"Verification also gets a DISPOSABLE instance of the product…"*.
The disposable stack (`open_run_check_environment`, `verify_environment.py`) is opened only by
`settle_client_attach` (`_loop_context.py:512-531`). Server runs — every BSVibe-on-BSVibe run — verify in the
plain sandbox. This sentence is wrong for them today.

## 2. Why it misses what it misses

Every stage that **reads** (judge, ledger, scope, review) reads a clipped view with no tools. Every stage that
**executes** runs either the agent's own tests or the repo's existing suite. So:

* **The agent's tests share the agent's blind spots.** 11-12 → 11-17, every real high the review found had passed
  the loop with green tests and a passing ledger:

  | Card | Defect | Why the loop passed it |
  |---|---|---|
  | #1386 r1 | sweep function never called in production | its test called it directly |
  | #1397 r1 | committed the caller's transaction | the test checked only the race |
  | #1404 r2 | concurrent items got an empty file list | the test judged one item at a time |
  | #1412 r2 | names inside docstrings counted as calls | no such input in the tests |

* **The checks that could catch these cannot act.** The review saw them but is report-only and reaches only the
  founder; each needed a human denial to get back into the loop (most re-work rounds this week).
* **The reviewer cannot look things up, so it guesses.** It saw only the diff: the same false high was raised three
  times on #1411 because `_hits` (outside the diff) already did what it claimed was missing. Other false findings
  this week: a REST handler "not catching" an error it already turned into 400; `MissingGreenlet` under a session
  setting prod never uses.
* **The one executing stage is shallow.** Demonstration probes are one-liners planned without tools, in a sandbox
  with no database or running services; a probe that cannot run is downgraded, not failed.

A careful engineer (or a good Claude Code session) does four things this loop does not: **open the code it is
unsure about, run the change through the real entry point, try to break it, and treat a reproduced failure as
work, not as a comment.**

## 3. Design

### 3.1 Principle: a finding must be reproduced to count

A reviewer that reads and guesses produces false highs; a reviewer that must **produce a failing check** cannot.
So every adversarial finding carries an executable reproduction (a test file or a command + expected output).
The harness runs it:

* reproduction **fails on the change** ⇒ a **confirmed** finding ⇒ fed back as a round failure;
* reproduction passes, or cannot run ⇒ **unconfirmed** ⇒ shown on the card as a suspicion, never fed back.

This is also the false-positive filter: an LLM can claim anything, but it cannot make a passing test fail.

Two rules keep that filter honest (stage 1 is still report-only; `passed` never changes):

* **Baseline control.** A reproduction that shows the defect after the change is run again on a copy of the tree
  *before* the change. It is **confirmed** only if it does not hold there. Same result before ⇒ unconfirmed
  ("변경 전에도 같은 결과"). A test that cannot even be collected before (it imports what the change adds) may be
  confirmed, with that reason recorded; a command reproduction that cannot run before is never confirmed; a baseline
  timeout never confirms.
* **Same environment as the gates.** Reproductions run through `uv run` (commands as `uv run sh -c ...`, tests as
  `uv run python -m pytest ...`), never a bare `python`. A reproduction that dies with an import / collection error is
  unconfirmed with the reason "재현이 실행되지 않음" (`not_run`), told apart from "the claim was wrong".

* **Expected basis x intent (confirmed = changed-only AND justified).** The baseline control proves "this change
  changed this behaviour", not "the changed behaviour is wrong": a behaviour the request told the agent to change
  differs before and after by definition, so a repro that expects the old behaviour always holds "only after the
  change". A confirmed candidate must therefore pass two more checks. They can only remove a confirmation, never add one:
  1. **Basis (`expected_from`).** Each finding states where the "correct behaviour" its repro asserts comes from, as one of
     `request` (a verbatim `quote` of the request body or a ledger item, with its `req_id`), `existing_test`
     (`path::name` of a repo test that passed before the change; the repro is that test, or that test fails after the
     change) or `unchanged_contract` (a `file:line` in the code *before* the change, for behaviour the request did not
     touch). The harness checks it mechanically: the quote must occur in the request body or ledger text (whitespace
     normalised, substring) and be at least 20 characters and 3 words long after normalising (a shorter one, such as
     `e` or `the`, occurs in any text and proves nothing: "기대 근거 불일치"); the existing test must pass on the pre-change tree and fail on the changed tree (unless it
     is the repro); an `unchanged_contract` location must be `file:line`. No basis: unconfirmed, "기대 근거 없음".
     Failed check: unconfirmed, "기대 근거 불일치".
  2. **Intent conflict (one judge call per candidate).** Candidates that pass 1 go to a separate call on the same judge
     path, given the request and the ledger: does the behaviour the repro expects conflict with what the request asked
     to change? Conflict: unconfirmed, "요청된 변경과 충돌(의도된 동작)". A failed or unreadable judge call leaves the
     candidate unconfirmed (conservative). Its tokens count toward the round's tokens, and the call is made only inside
     the round's token cap: if what is left is less than one call needs (prompt estimate plus the answer), no call is
     made and the candidate stays unconfirmed, "토큰 예산 소진으로 의도 충돌 판정을 하지 못했다".

  This closes the "request-mandated change confirmed as a regression" class (#1440, replay `9c8254c5`), which section 4
  needs at about zero wrongly-confirmed before Stage A may gate.

### 3.2 Stage A — Adversarial verifier (new, inside the round)

* **Where:** in `VerificationService.verify`, after the derived gate (VS:809-866), before the ledger (VS:871).
  Runs only when the gate passed (no point attacking a red tree).
* **Who:** a separate agent, not the one that wrote the change, with **read and run tools in the same sandbox**:
  `file_read`, `grep`, `shell_exec` (reuse the MCP work tools / `SandboxSession.exec`). No write access to
  non-test files; it may create files only under a scratch test directory that the harness removes afterwards.
* **Brief:** the request, the ledger, the diff, the call-site map — and an explicit checklist drawn from this
  week's real misses: *Is every new function reachable from a production entry point? Does it commit/rollback a
  session it does not own? What happens under concurrency? Under the boundary inputs the request names? Does it
  regress an existing behaviour the diff touches?*
* **Output:** `{findings: [{severity, claim, file, line, repro: {kind: test|command, path|command, expect}}]}`,
  at most N (e.g. 5) findings, each with a reproduction.
* **Harness:** runs each reproduction against the change (deterministic, exit code / expected output). Confirmed
  findings make `passed = False` and go to the agent through `verification_feedback._sections()` as
  `ADVERSARIAL CHECK FAILED: <claim>\n<repro>\n<output>`, with the reproduction file kept so the agent can run it.
* **Budget:** one verifier session per round, time- and token-capped like the review; capped at K rounds per run
  so a run cannot ping-pong forever (after K, findings become card-only). **K defaults to 2 and is a product
  setting the owner can change in the product's settings (PWA) and over MCP / REST** (decision 11-17).

### 3.2.1 Stage A for deliverables that are not code (reports, documents, analyses, messages)

BSVibe's work is not only code (the weekly BStockReport is a report; other runs produce documents, analyses,
messages). The principle carries over unchanged — **a finding counts only when a check the harness can run
contradicts the deliverable** — only the kind of reproduction changes:

| Deliverable | What the verifier attacks | Reproduction the harness runs |
|---|---|---|
| A report built from data (numbers, tables) | every figure and the claims drawn from it | re-run the source command / query and compare the value; recompute a derived figure from the raw rows |
| A document about the system (runbook, design, README) | every concrete reference: paths, functions, commands, settings, links | `test -e path`, `git grep -n name`, `--help` / dry-run of the command, an HTTP HEAD on the link |
| A message or summary for a person | that it says what the request asked and nothing the sources do not support | quote → source lookup (the sentence's figure or name must appear in a cited source) |
| Any deliverable with explicit constraints (length, format, "no X") | each constraint | a deterministic check (word count, schema validation, banned-phrase grep) |

Judgement calls that no command can settle (tone, emphasis, "is this the right analysis") stay **unconfirmed**:
shown on the card, never fed back. The verifier is told which surface it is on — the same split the outcome
demonstration already makes (`code` / `artifact`, `outcome_demonstration.py`) — and gets the matching checklist.

### 3.3 Stage B — The review becomes the verifier's last word, not a separate report

Once Stage A exists, the post-PASS review (stage 9) is redundant as a separate tool-less call. Replace it with
Stage A's **unconfirmed** findings plus a one-line summary of what was attacked and survived. The card shows:
confirmed-and-fixed findings (from earlier rounds), surviving suspicions, and what was tried.

### 3.4 Stage C — The agent verifies behaviour itself, before spending a round

* Brief change (`agent_briefing.py`): after lint/type/test, **exercise the change through its real entry point**
  (call the API route with the test client, run the worker function, run the CLI) and **write the test that would
  have caught the bug you fixed**. Name the reachability question explicitly.
* Fix the false sentence in `agent_briefing.py:95-100`: either wire the disposable stack into server verification
  (Stage D) or stop promising it.

### 3.5 Stage D — Wire the disposable stack into server verification (from the 08-10 design)

`open_run_check_environment` exists and works for client_attach. Make it available to the demonstration probes
and to Stage A when the product declares a stack, so "hit the endpoint" means a real backend + database, not
`python -c`. Slot accounting (`verify_slots`) and the "second stack can kill prod" warning
(`production-verification.md` §3.3.1) apply unchanged.

### 3.6 What stays

The derived gate, `final` commands, the ledger and the founder's Safe Mode card stay. The ledger keeps answering
"was every requirement addressed"; Stage A answers "does it actually hold up". The founder still approves —
but sees findings that were reproduced, not guesses.

## 4. Order of work (each a BSVibe issue)

1. **Stage A, report-only first** — the verifier with tools + reproduction harness, both surfaces (code §3.2 and
   artifact §3.2.1); confirmed / unconfirmed shown on the card and in run detail; **does not fail the round yet**.
   Measure: confirmed rate, false-confirmed rate (a confirmed finding the founder judges wrong), cost per round.
2. **Stage A gates the round** — confirmed findings fail the round and go to the agent; K-round cap as a product
   setting (default 2) in the PWA product settings and over MCP / REST.
   Only after (1) shows a false-confirmed rate near zero over ~20 runs.
3. **Stage B** — retire the tool-less review; the card shows Stage A's results.
4. **Stage C** — briefing: entry-point exercise + "write the test that would have caught it"; fix the stack sentence.
5. **Stage D** — disposable stack for server-run probes and Stage A (largest; needs its own design check against
   prod safety).

## 5. How we will know it works

* **Founder denials per card** (today: most re-work this week came from a human reading the review) should fall.
* **Real highs found after PASS** (on the card) should fall; confirmed findings found **inside** the loop rise.
* **False findings on the card** should approach zero, because only reproduced ones are asserted.
* Round-1 pass rate may *drop* at first — the loop is stricter. Watch total rounds-to-ship, not round 1 alone.

## 6. Decisions (형님, 2026-11-17)

1. **Cost is accepted**: "안정성, 신뢰성이 bsvibe의 핵심철학이니 진행해" — Stage A runs on every passed-gate
   round, on the user's own tokens.
2. **Not code only**: Stage A covers every deliverable, with the artifact checklist of §3.2.1 — not a per-product
   skip.
3. **K = 2 by default, configurable**: a product setting, editable in the product's settings screen and over
   MCP / REST.
