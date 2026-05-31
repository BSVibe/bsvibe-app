"""Routing classifiers — pick a tier (local / cloud / mixed) for a request.

- :class:`StaticClassifier` — heuristic over token count + tool count +
  code-block count. Fast, deterministic, no LLM cost.
- :class:`LocalVsCloudClassifier` — 2-tier wrapper. Static score outside
  the gray band routes immediately; gray-zone scores defer to a slower
  classifier (e.g. a small LLM) so cheap chores stay local while
  substantial work goes cloud.
"""

from __future__ import annotations

from backend.router.classifier.base import (
    ClassificationFeatures,
    ClassificationResult,
    Classifier,
    Tier,
)
from backend.router.classifier.local_vs_cloud import LocalVsCloudClassifier
from backend.router.classifier.static import StaticClassifier

__all__ = [
    "ClassificationFeatures",
    "ClassificationResult",
    "Classifier",
    "LocalVsCloudClassifier",
    "StaticClassifier",
    "Tier",
]
