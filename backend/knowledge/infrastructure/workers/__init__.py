"""Knowledge context — infrastructure workers.

Per v8 D34. The settle worker is the BSage write subscriber that drains
``settle``-class :class:`~backend.workflow.infrastructure.db.ExecutionRunActivity` rows
into each workspace's vault (the *learning half* of the §5 trust ratchet).
"""

from __future__ import annotations
