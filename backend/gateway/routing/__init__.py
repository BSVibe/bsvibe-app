"""Routing domain (Bundle 1.5b) — catalog, logs, registry, strategies."""

from backend.gateway.routing.catalog_repository import (
    ModelCatalogDuplicateError,
    ModelCatalogRepository,
)
from backend.gateway.routing.db import (
    GatewayRoutingBase,
    ModelCatalogEntryRow,
    RoutingLogRow,
)
from backend.gateway.routing.logs_repository import (
    RoutingLogFeatures,
    RoutingLogsRepository,
)
from backend.gateway.routing.registry import (
    DbModelRow,
    ModelCatalogReadRepo,
    ModelEntry,
    ModelRegistryService,
)
from backend.gateway.routing.strategies import (
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
