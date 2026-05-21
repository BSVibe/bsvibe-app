"""Composer — knowledge fragment retrieval for work LLM prompt assembly.

The legacy ``KnowledgeClient`` HTTP wrapper (BSNexus→BSage) was retired
when BSage became in-process under ``backend.knowledge``. Callers
needing knowledge retrieval should depend on
``backend.knowledge.retrieval`` directly.
"""
