"""executor_task_agentic — mark a dispatched task as an agent run or a chat turn.

BSVibe's first principle is that an executor account and a LiteLLM account behave
IDENTICALLY through the ``chat()`` abstraction. A LiteLLM call with no tools
cannot inspect anything — it answers from the prompt. The executor did not match:
EVERY task, chat turns included, booted the full agentic CLI with tool access in
an empty per-task temp dir, so a founder's question ("현 프로젝트 상황 설명해줘")
came back as a description of that temp dir (prod, 2026-07-13), and async answers
timed out at 300 s inside the tool loop.

``agentic`` carries the caller's ``tools`` argument down to the worker: tools → an
agent run (sandbox tools on), no tools → a plain completion (tools off).

Defaults to TRUE so in-flight rows and any older backend keep the agent-run
behaviour — the coding loop must never silently lose its tools.

Revision ID: executor_task_agentic
Revises: run_routing_source_text
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "executor_task_agentic"
down_revision: Union[str, None] = "run_routing_source_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_tasks",
        sa.Column(
            "agentic",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("executor_tasks", "agentic")
