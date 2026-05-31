"""Plugin-author-facing runtime data carriers.

The :class:`Context` shape published here is the *SDK* view of what
plugin capability calls receive — a deliberately minimal projection of
the engine's ``SkillContext`` (``backend.extensions.plugin.context``).
The runtime carrier carries the same field names; plugins import
``Context`` from the SDK to type-annotate their capability functions.

:class:`Result` is a tiny success/error envelope plugin actions may
return. Use of ``Result`` is *optional* — the runtime accepts any
serializable shape — but typed plugins benefit from a canonical
two-state return.

Lift S keeps the data carriers Protocol-light and dependency-free.
Lift R will move the canonical implementations into ``bsvibe_sdk``;
until then the carrier is a structural duck-type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Context:
    """Plugin capability call context (SDK projection of ``SkillContext``).

    Fields mirror what the engine injects so plugins can type-hint
    their capability signatures::

        from bsvibe_sdk import Context

        async def open_pr(context: Context, branch: str, title: str) -> None:
            context.logger.info("opening_pr", branch=branch)
            ...

    The runtime carrier (``backend.extensions.plugin.context.SkillContext``)
    is a superset; plugins should only rely on the fields published here.
    """

    logger: Any
    config: dict[str, Any] = field(default_factory=dict)
    credentials: dict[str, Any] = field(default_factory=dict)
    input_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class Result:
    """Optional success / error envelope for plugin action returns.

    Construct via :meth:`ok` or :meth:`err`::

        return Result.ok({"pr_number": 42})
        return Result.err("repo not found")
    """

    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None

    @classmethod
    def ok(cls, data: dict[str, Any] | None = None) -> Result:
        return cls(success=True, data=data, error=None)

    @classmethod
    def err(cls, message: str) -> Result:
        return cls(success=False, data=None, error=message)


__all__ = ["Context", "Result"]
