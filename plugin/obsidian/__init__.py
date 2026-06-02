"""Obsidian connector plugin — vault knowledge import (Lift Q3-Obsidian).

Inbound knowledge ingestion: founder points the plugin at a local Obsidian
vault directory and the ``import_vault`` action walks every markdown note,
parses frontmatter + body, and submits each as a ``write_seed`` call on the
plugin-restricted garden surface. ``IngestCompiler`` (downstream) classifies
the seeds into garden notes.

Capabilities:

* ``@p.setup`` — record vault root path + optional exclude globs + default
  region; no credentials needed (the vault lives on the same host as the
  worker).
* ``@p.action("import_vault")`` — agent-loop tool: scan vault → parse
  frontmatter → call ``context.knowledge.write_seed`` per note → log
  ``audit.knowledge.imported.obsidian`` summary.

Read-only against the founder's local filesystem; all external I/O is the
``write_seed`` calls back into the BSage knowledge subsystem. No outbound /
compensate / inbound-webhook capabilities — Obsidian is a one-way ingest.
"""

from __future__ import annotations

# Lift N defensive pattern #1 (v8 §22) — namespace-only, no re-exports.
__all__: list[str] = []
