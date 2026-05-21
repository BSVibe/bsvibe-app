"""G10 — CoT decomposer.

The hard-coded N=1 ``plan_and_dispatch_request`` path (G9) means every
Request becomes exactly one WorkStep. For anything non-trivial the
model runs out of phase budget before it can satisfy the verifier on
all aspects. G10 inserts a *single* extra LLM call before the WorkPlan
is built: same model, no tools, asks the LLM "is this work simple
enough for one step, or does it have natural checkpoints?". The reply
is a JSON array of step drafts.

There is no new agent role. The decomposer is the same work LLM doing
its first turn of reasoning *before* the WorkStep loop starts —
analogous to a human engineer scanning a ticket and deciding "yeah,
this is one PR" vs "this needs three commits". From the founder
surface (Direction / Decide / Review) nothing changes: Brief still
groups by Request.

Public API:

    ctx = await build_project_context(request=…, session=…,
                                       knowledge_client=…)
    steps = await decompose_request(ctx, executor=…, model=…)
"""

from backend.execution.planning.context import ProjectContext, build_project_context
from backend.execution.planning.decomposer import decompose_request

__all__ = ["ProjectContext", "build_project_context", "decompose_request"]
