# BSVibe session handoff — 2026-09-30 (evening)

**Deploy topology** — they differ. Mixing them up costs an hour of "I deployed, why didn't it change".

| | How it ships |
|---|---|
| **Container stack** (backend · worker) | ✅ **autodeploy every 2 min** — a **separate block** in `_infra/scripts/autodeploy.sh` (line ~171). It is not in the `PROJECTS` array (easy to misread as "not deployed"). State: `_infra/logs/bsvibe-app.deployed` |
| **PWA** | Vercel on merge to main |
| **Host executor workers (2)** | 🔴 **not autodeployed.** `git pull` on `main`, then `launchctl kickstart -k gui/501/com.bsvibe.worker-{admin,mac-mini-e2e}` and check the **process start time**. If you changed the plist: `bootout` + `bootstrap` |

⚠️ **A PR that touches both frontend and backend opens a window** — Vercel ships at once, the backend poller every 2 min.

**Open work lives in GitHub issues, not in this document.**
**How the whole system works: [SYSTEM_OVERVIEW.md](./SYSTEM_OVERVIEW.md)** — read §4 before touching anything about executors.

> 🧭 **Never put anything transitive in this header.** A field that points at itself becomes
> false the moment it merges — updating the value does not fix it; **removing the field** does.

> 🐙 **Opening an issue or a PR on `BSVibe/bsvibe-app` now starts a prod run** (§Ⅱ). To file
> record-only issues/PRs, pause the binding filter first (§Ⅳ) — and ask 형님 each time.

---

## §0 — One line: **the agent was capable; the harness around it was the limit**

The GitHub App path went live and ran end to end twice (issue → run → Safe Mode → PR →
merge-watch auto-merge). Then a real engineering issue (#1102, a concurrency bug) stopped:
the framer split it into two chained runs, the first Claude Code session wrote a correct red
test in 7 min and **3.06M prompt tokens**, and the per-run ceiling (2M) halted the chain
before the fix. Claude Code did exactly what it was told. BSVibe's wrapper — step split,
tool restriction, a fresh session per round, a ceiling that counts cache reads at full weight
and fires after the session — is what failed. That became the briefing
([SYSTEM_OVERVIEW.md](./SYSTEM_OVERVIEW.md)) and issues #1102–#1116.

## §Ⅰ — What this session produced

| | |
|---|---|
| **Merged** | #1095 RLS fail-closed retry (③) · #1096 handoff · #1098 GitHub App webhook ingress `POST /api/webhooks/github` + manifest `issues`/`issue_comment` · #1100 (BSVibe-made, from issue #1099) · #1101 skip PRs from BSVibe's own branches · #1117 `docs/SYSTEM_OVERVIEW.md` |
| **Issues filed** | #1102–#1116 (map in [SYSTEM_OVERVIEW.md §10.3](./SYSTEM_OVERVIEW.md)) |
| **Notion** | Handoff archive was **eight sessions behind** (last archived 09-21 pm). All eight moved as verbatim `.md` attachments |

### ③ fail-closed — done this time
* Pre-deploy differential canary on a fresh prod dump: open = closed = zero row growth
* **Positive control on the same dump:** pre-fix code (`2fd0c95`) closed → intake 50 · claim 10 per tick (the 09-29 incident). The canary still discriminates
* Local fail-closed full suite 7256 passed; blind-statement census: zero negation sites
* Post-deploy 10 min + ~12 h of 30-min checks: `rls_fail_closed`, no row growth beyond human-made runs, zero errors

## §Ⅱ — Prod right now

| | |
|---|---|
| `alembic_version` | `rls_fail_closed` |
| GitHub ingress | App `bsvibe` has Issues read + Issues/Issue comment events (형님 changed them 09-30). Binding `c50e7217…` (`BSVibe/bsvibe-app` → product BSVibe) has `trigger.filters = {}` → **every opened/edited issue and every PR from a non-`bsvibe/run…` branch becomes a run**, outside the run cap and token budget (#1113) |
| Runs in flight | none |

## §Ⅲ — Next

1. **Decide how BSVibe wraps Claude Code** ([SYSTEM_OVERVIEW.md §4](./SYSTEM_OVERVIEW.md), §10.3). Most of #1103 · #1104 · #1106 · #1114 resolve differently depending on it — decide before fixing any of them. 형님 said on 09-30 he wants to understand the internal design first; the overview is the starting point.
2. **#1113 opt-in** — which issues become work. `trigger.filters` already supports key-equality (proven today); a label-based filter is the cheapest interim.
3. Correctness issues that do not depend on (1): #1102 / #1110 (one state machine with DB compare-and-set), #1107 (sandbox shared per product — reproduce first), #1108, #1111, #1112, #1105.
4. Carried over: credential rotation ×3 · #1047 · #937 cold-boot · #1042 · #954 · #949 · #957.

## §Ⅳ — Discipline that paid off

* **🐙⭐⭐ Filing issues/PRs on bsvibe-app is now a prod action.** Record-only: set the binding filter to a non-matching value (`bsvibe_bindings_update`, `{"filters":{"github_event":"__paused__"}}`), confirm the trigger row carries `_received_filtered` and `requests` did not grow, then restore `{"filters":{}}`. The auto-mode classifier blocks the binding change unless 형님 OKs it in the conversation.
* **🛑⭐⭐ Cancelling a run does not stop its session.** A run cancelled at 10:41 kept its Claude Code session alive until 10:52 (6.4M tokens) with its run-scoped MCP token still valid (#1106). Cancel only before the executor task exists (~25 s after the request), or expect to pay.
* **🔬⭐⭐ Canary with a positive control, on the same snapshot.** A "no change" canary is only evidence if the known-bad code turns it red on that very dump.
* **🧾⭐ Re-verify subagent reports before acting.** Of the audit claims turned into issues, one was wrong ("`output_mode` has no reader" — `delivery_worker` reads it; the real defect was #1108).
* **🪞 A source-text guard matched my own docstring.** Cutting the call left the guard green because the helper's docstring named the function; narrowed to the call form `_resolve_inbound_product(`.
* **🗄 `SELECT DISTINCT` over a JSON column fails on Postgres** (`could not identify an equality operator for type json`) — SQLite never tells you. Use `IN (subquery)`.
* **🧯 Nothing was committed in the deploy directory.** Every change went through a worktree.
