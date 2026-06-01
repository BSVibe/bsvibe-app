"""Router domain layer — pure Protocols + entities.

Lift I-Repo-Router. Mirrors the Workflow context's domain/infrastructure split
(v8 D44/D45): application code depends on Protocols defined here, never on
``sqlalchemy.*`` directly. Concrete SQL implementations live in
:mod:`backend.router.infrastructure.repositories`.
"""
