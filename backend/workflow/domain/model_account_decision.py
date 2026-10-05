"""The Decisions a run stops on when it cannot pick a model account (#1074).

Leaf-level so both sides can read them: the runtime that raises the Decision
(``runtime.account_resolution``) and the checkpoint resolve that answers it
(``checkpoint_resolution``, which the MCP surface reaches and which must not
import the runtime graph).
"""

from __future__ import annotations

DECISION_NO_MODEL_ACCOUNT = "no_model_account"
DECISION_AMBIGUOUS_MODEL_ACCOUNT = "ambiguous_model_account"
#: Decision payload key mapping each offered option to its model account id.
ACCOUNT_CHOICES_KEY = "account_choices"

__all__ = [
    "ACCOUNT_CHOICES_KEY",
    "DECISION_AMBIGUOUS_MODEL_ACCOUNT",
    "DECISION_NO_MODEL_ACCOUNT",
]
