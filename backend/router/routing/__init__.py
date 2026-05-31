"""Routing domain (Bundle 1.5b) — catalog, logs, registry, strategies."""

from backend.router.routing.catalog_repository import (
    ModelCatalogDuplicateError,
    ModelCatalogRepository,
)
from backend.router.routing.db import (
    GatewayRoutingBase,
    ModelCatalogEntryRow,
    RoutingLogRow,
)
from backend.router.routing.logs_repository import (
    RoutingLogFeatures,
    RoutingLogsRepository,
)
from backend.router.routing.registry import (
    DbModelRow,
    ModelCatalogReadRepo,
    ModelEntry,
    ModelRegistryService,
)
from backend.router.routing.strategies import (
    ABTestConfig,
    ABTester,
    CostOptimizationConfig,
    CostOptimizer,
    RegionConfig,
    RegionSelector,
)

__all__ = [
    "ABTestConfig",
    "ABTester",
    "CostOptimizationConfig",
    "CostOptimizer",
    "DbModelRow",
    "GatewayRoutingBase",
    "ModelCatalogDuplicateError",
    "ModelCatalogEntryRow",
    "ModelCatalogReadRepo",
    "ModelCatalogRepository",
    "ModelEntry",
    "ModelRegistryService",
    "RegionConfig",
    "RegionSelector",
    "RoutingLogFeatures",
    "RoutingLogRow",
    "RoutingLogsRepository",
]
