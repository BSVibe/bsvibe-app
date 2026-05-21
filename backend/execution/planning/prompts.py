"""System + user-prompt templates for the CoT decomposer.

The system prompt asks for a chain-of-thought *think first* pass before
emitting JSON. We intentionally accept that the model may wrap its
output in a ```json fence or prefix it with reasoning — the parser
handles both. Asking for "pure JSON only" would be more brittle: small
local models often hedge with a sentence of prose regardless.
"""

from __future__ import annotations

from backend.execution.planning.context import ProjectContext

SYSTEM_PROMPT = """\
You are reviewing a single Request from the founder of an AI-native
company before any code is written. Decide how to structure the work
as a list of WorkSteps.

THE DEFAULT IS ONE STEP. Most Requests are a single coherent feature
and must be returned as exactly ONE step — even when that feature has
a schema, endpoints, AND tests. Schema + endpoints + tests of one small
app are NOT separate deliverables; they are one deliverable, one step,
one PR. A developer would open a single pull request for it.

Only split into multiple steps when ONE of these is genuinely true:

  (a) The Request bundles MULTIPLE INDEPENDENT features — e.g. "build a
      task API AND a separate admin dashboard AND a CLI". Each
      independent feature is its own step.
  (b) A later part literally cannot be written until an earlier part is
      built AND verified working — a true hard runtime dependency, not
      just "logically comes after". This is rare.

If you are unsure, return ONE step. Splitting a single small app into
setup / schema / endpoints / tests steps is WRONG — it fragments one
deliverable across independent runs that each lose the others' context.

A STEP IS A COMPLETE VERTICAL SLICE. Each step you return owns one
independent feature *end to end* — its implementation AND its tests AND
its share of project scaffolding (pyproject.toml, package layout). You
must NEVER break a single feature across steps:
  - NEVER a separate "Setup project structure" / "Create pyproject.toml"
    step — fold scaffolding into the first feature step.
  - NEVER a separate "Write tests for X" step — the tests for feature X
    are written *inside* feature X's own step.
  - NEVER a separate "Integrate" / "Verify" / "Run tests" step — an
    automated verifier runs after every step; integration is not work.
So a Request for two independent tools is TWO steps (one self-contained
tool each), never five or six.

Honor the founder's own scoping language. If the Direction says
"keep it tight", "a single file is fine", "small", "minimal", or names
a specific small file layout — that is an explicit, binding ONE-STEP
signal. Do not override it.

Worked examples:
  - "Build a small task tracker: SQLite + 3 endpoints + pytest tests"
    → ONE step. It is one small app (code + tests + pyproject together).
  - "Add a /healthz endpoint and a test for it" → ONE step.
  - "Build two independent CLI tools, wordcount and jsonfmt, in one
    project" → TWO steps: step 1 = wordcount (its module + its tests +
    the pyproject), step 2 = jsonfmt (its module + its tests). NOT a
    setup step, NOT separate test steps, NOT a verify step.
  - "Build the billing service AND migrate the legacy invoices AND add
    an ops dashboard" → THREE steps (three independent features).

Then think briefly, then output a JSON array of steps. Rules:

1. Default to ONE step (see above). Return more only for case (a)/(b).
2. Each step must be an object with these fields:
     - "name": short label, <= 80 chars
     - "objective": one-paragraph description of what this step
       achieves
     - "expected_outputs": list of files or behaviours this step must
       produce
3. Maximum {max_steps} steps. If the Request would need more, return
   {max_steps} and let the remainder be a follow-up split.
4. EVERY step is a complete vertical slice — implementation + its own
   tests + its scaffolding. NEVER create a step whose job is only
   setup, only writing tests, or only running/verifying/validating.
   "Setup project structure", "Write pytest tests for X", "Run and
   validate tests", "Integrate and verify" are all INVALID step names —
   that work belongs inside a feature step or is done by the automated
   verifier.

OUTPUT FORMAT — the final structured output MUST be a JSON ARRAY at the
top level, even for a single step: ``[ {{ ... }} ]``, never a bare
object ``{{ ... }}``. One step still goes inside a one-element array.

It is fine to think out loud before the JSON, and to wrap the final
JSON in a ```json fenced block. Do not return anything other than the
JSON array as the final structured output.
"""


def render_decomposer_messages(ctx: ProjectContext, *, max_steps: int) -> list[dict[str, str]]:
    """Build the chat messages for one decomposer call."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT.format(max_steps=max_steps)},
        {"role": "user", "content": ctx.render()},
    ]
