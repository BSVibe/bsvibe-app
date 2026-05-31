"""Built-in BSVibe plugin implementations.

Each subdirectory holds one plugin: ``<name>/plugin.py`` defines a single
``backend.extensions.plugin.PluginBuilder`` via the ``plugin(...)`` factory and the
capability decorators. The :class:`backend.extensions.plugin.PluginLoader` scans this
directory at startup — no central registry edit is needed to add a plugin.
"""

from __future__ import annotations
