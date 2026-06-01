"""BSVibe backend root package.

Namespace-only — the public surface lives under the six bounded contexts
(``router`` · ``knowledge`` · ``workflow`` · ``identity`` · ``schedule`` ·
``extensions``) plus the common leaf modules. Lift N (v8 §22 #1) adds
explicit ``__all__`` markers across every backend sub-package.
"""

from __future__ import annotations

# Namespace-only package — no top-level re-exports. Callers import from
# the explicit sub-package surface (Lift N defensive pattern #1 / v8 §22).
__all__: list[str] = []
