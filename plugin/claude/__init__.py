"""Claude conversation export connector — knowledge import (Lift Q3-Claude).

Imports the JSON export produced by claude.ai → Settings → Privacy →
Export Data. Each conversation in ``conversations.json`` is rendered to
markdown and seeded via the restricted ``context.knowledge.write_seed``
surface so :class:`IngestCompiler` classifies it on the next compile pass.
"""
