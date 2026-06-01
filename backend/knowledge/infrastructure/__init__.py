"""Knowledge context — infrastructure layer.

Currently holds the settle worker (the BSage write subscriber that drains
``settle``-class run activities into each workspace's vault). Repository
extraction lives in Lift I.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — public surface lives in nested modules.
__all__: list[str] = []
