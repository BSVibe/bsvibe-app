"""Edge lifecycle — promotion (weak→strong) and demotion (strong→weak) of graph edges."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from backend.knowledge.graph.graph_store import GraphStore

logger = structlog.get_logger(__name__)


@dataclass
class EdgeLifecycleConfig:
    """Configuration for edge promotion and demotion."""

    promotion_min_mentions: int = 3
    demotion_days: int = 90
    weak_weight: float = 0.1
    promoted_weight: float = 0.8


class EdgeLifecycleEvaluator:
    """Evaluates edges for promotion (weak→strong) or demotion (strong→weak).

    Promotion: A weak edge (body mention) that appears in N+ distinct notes
    gets promoted to strong with elevated weight.

    Demotion: A strong edge not referenced in any note for N days gets
    demoted to weak with reduced weight.
    """

    def __init__(self, store: GraphStore, config: EdgeLifecycleConfig | None = None) -> None:
        self._store = store
        self._config = config or EdgeLifecycleConfig()

    async def find_promotion_candidates(self) -> list[dict[str, Any]]:
        """Find weak-edge targets mentioned from N+ distinct source notes.

        A target entity qualifies for promotion when it has weak edges
        from N+ distinct source_paths (i.e., mentioned in N+ notes).

        Returns:
            List of dicts with: target_name, mention_count.
        """
        sql = """
            SELECT e_tgt.name AS target_name,
                   COUNT(DISTINCT r.source_path) AS mention_count
            FROM relationships r
            JOIN entities e_tgt ON e_tgt.id = r.target_id
            WHERE r.edge_type = 'weak'
            GROUP BY e_tgt.name
            HAVING mention_count >= ?
        """
        rows = await self._store.query(sql, (self._config.promotion_min_mentions,))
        return [{"target_name": row[0], "mention_count": row[1]} for row in rows]

    async def find_demotion_candidates(self) -> list[dict[str, Any]]:
        """Find strong edges that haven't been referenced recently.

        A strong edge qualifies for demotion when its created_at is older than
        demotion_days and no recent weak edges reinforce it.

        Returns:
            List of dicts with edge info: rel_id, source_name, target_name, days_stale.
        """
        sql = """
            SELECT r.id,
                   e_src.name AS source_name,
                   e_tgt.name AS target_name,
                   CAST(julianday('now') - julianday(r.created_at) AS INTEGER) AS days_stale
            FROM relationships r
            JOIN entities e_src ON e_src.id = r.source_id
            JOIN entities e_tgt ON e_tgt.id = r.target_id
            WHERE r.edge_type = 'strong'
              AND CAST(julianday('now') - julianday(r.created_at) AS INTEGER) >= ?
        """
        rows = await self._store.query(sql, (self._config.demotion_days,))
        return [
            {
                "rel_id": row[0],
                "source_name": row[1],
                "target_name": row[2],
                "days_stale": row[3],
            }
            for row in rows
        ]

    async def promote_edges(self) -> int:
        """Promote qualifying weak edges to strong. Returns count promoted."""
        candidates = await self.find_promotion_candidates()
        if not candidates:
            return 0
        statements = [
            (
                """UPDATE relationships
                   SET edge_type = 'strong', weight = ?
                   WHERE edge_type = 'weak'
                     AND target_id IN (
                         SELECT id FROM entities WHERE name = ?
                     )""",
                (self._config.promoted_weight, candidate["target_name"]),
            )
            for candidate in candidates
        ]
        promoted = await self._store.execute_batch(statements)
        if promoted:
            logger.info("edges_promoted", count=promoted)
        return promoted

    async def demote_edges(self) -> int:
        """Demote qualifying strong edges to weak. Returns count demoted."""
        candidates = await self.find_demotion_candidates()
        if not candidates:
            return 0
        statements = [
            (
                """UPDATE relationships
                   SET edge_type = 'weak', weight = ?
                   WHERE id = ?""",
                (self._config.weak_weight, candidate["rel_id"]),
            )
            for candidate in candidates
        ]
        demoted = await self._store.execute_batch(statements)
        if demoted:
            logger.info("edges_demoted", count=demoted)
        return demoted
